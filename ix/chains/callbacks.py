import logging
import time
import traceback
from collections import defaultdict
import dataclasses
from functools import cached_property
from typing import Dict, Union, Any, List, Optional
from uuid import UUID

from channels.layers import get_channel_layer
from django.db.models import Q

from ix.schema.subscriptions import ChatMessageTokenSubscription
from langchain.callbacks.manager import AsyncCallbackManagerForChainRun

from ix.chat.models import Chat
from langchain.callbacks.base import AsyncCallbackHandler
from langchain.schema import AgentAction, BaseMessage

from ix.agents.models import Agent
from ix.chains.models import Chain
from ix.task_log.models import Task, TaskLogMessage


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class RunContext:
    """Context info for a single run of an llm/chain."""

    # Whether the current chain is streaming.
    is_streaming: bool = False

    # Message generated by the chain. Used to cache message metadata
    # required to stream the message to the client. Assumes a IxHandler/run_manager
    # is used only once.
    message: Any = None

    # cache of tokens
    tokens: list = dataclasses.field(default_factory=list)

    async def finalize_stream(self):
        """
        Write the completed stream to the message.
        Updating the message notifies clients via django-channels.
        """
        if self.message is None:
            return
        self.message.content["stream"] = False
        self.message.content["text"] = "".join(self.tokens)
        await self.message.asave(update_fields=["content"])


def exception_to_string(excp: Exception) -> str:
    """Print a traceback to a string and return it"""
    stack_trace = traceback.extract_stack()[
        :-2
    ]  # Remove the call to exception_to_string from the stack trace
    if excp is not None:
        trace = traceback.extract_tb(
            excp.__traceback__
        )  # Get the traceback of the exception
        stack_trace = stack_trace + trace  # Add it to the stack trace
    else:
        stack_trace = stack_trace[:-1]  # Remove call to print_stack

    # HAX: incredibly hacky way to remove the celery stack trace from the error.
    #      this drops ~60 lines from the traceback.
    start_index = 0
    for i, trace in enumerate(stack_trace):
        if [trace.filename, trace.name] == [
            "/usr/local/lib/python3.11/site-packages/celery/app/trace.py",
            "__protected_call__",
        ]:
            start_index = i + 1
            break

    traceback_str = ""
    for i in stack_trace[start_index:]:
        traceback_str += (
            f'File "{i.filename}", line {i.lineno}, in {i.name}\n    {i.line}\n'
        )
    return traceback_str


class IxHandler(AsyncCallbackHandler):
    task: Task = None
    chain: Chain = None
    agent: Agent = None
    parent_think_msg = None

    # handlers are shared between run_managers. contexts store state
    # for the managers using run_id as the lookup.
    contexts: dict = None

    def __init__(self, agent: Agent, chain: Chain, task: Task, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = agent
        self.chain = chain
        self.task = task
        self.channel_layer = get_channel_layer()
        self.channel_name = f"{self.task.id}_stream"
        self.contexts = defaultdict(RunContext)

    @property
    def user_id(self) -> str:
        # HAX: this is currently always the owner of the chat. Likely need to update
        # this in the future to be the user making the request.
        return str(self.task.user_id)

    @cached_property
    def chat_id(self) -> str:
        try:
            chat = Chat.objects.get(Q(task=self.task) | Q(task_id=self.task.parent_id))
        except Chat.DoesNotExist:
            return None
        return chat.id

    async def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Any:
        """Runs when a chat model starts"""
        # connect to parent chain's run_id.
        context = self.contexts[parent_run_id]
        params = kwargs.get("invocation_params", {})
        _type = params.get("_type")

        if _type == "openai-chat":
            context.is_streaming = params.get("stream", False)

        # Send a placeholder message when starting a stream.
        if context.is_streaming:
            context.message = await self.send_agent_msg(stream=True)

    async def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> Any:
        """Runs when an LLM model starts"""
        pass

    async def on_llm_new_token(
        self, token: str, parent_run_id: Optional[UUID] = None, **kwargs: Any
    ) -> Any:
        """Stream tokens over django-channels to clients subscribed via graphql"""
        context = self.contexts[parent_run_id]
        # sometimes the first token is None
        if isinstance(token, str):
            context.tokens.append(token)
        await ChatMessageTokenSubscription.on_new_token(
            task=self.task,
            message_id=context.message.id,
            index=len(context.tokens),
            text=token,
        )

    async def on_llm_error(
        self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any
    ) -> Any:
        """Run when LLM errors."""

    async def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Run when chain starts running."""

        if not self.parent_think_msg:
            self.start = time.time()
            think_msg = await TaskLogMessage.objects.acreate(
                task_id=self.task.id,
                role="system",
                content={"type": "THINK", "input": inputs, "agent": self.agent.alias},
            )
            self.parent_think_msg = think_msg

    async def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> Any:
        # finalize stream if necessary
        context = self.contexts[run_id]
        if context.is_streaming:
            await context.finalize_stream()

        # only record the final thought for now
        if not parent_run_id:
            await TaskLogMessage.objects.acreate(
                task_id=self.task.id,
                role="system",
                parent_id=self.parent_think_msg.id,
                content={
                    "type": "THOUGHT",
                    # TODO: hook usage up, might be another signal though.
                    # "usage": response["usage"],
                    "runtime": time.time() - self.start,
                },
            )

    async def on_chain_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> None:
        """Run when chain errors."""
        await self.send_error_msg(error)

    async def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, **kwargs: Any
    ) -> Any:
        pass

    def on_agent_action(self, action: AgentAction, **kwargs: Any) -> Any:
        pass

    async def send_agent_msg(
        self, text: str = "", stream: bool = False
    ) -> TaskLogMessage:
        """
        Send a message to the agent.
        """
        return await TaskLogMessage.objects.acreate(
            task_id=self.task.id,
            role="assistant",
            parent=self.parent_think_msg,
            content={
                "type": "ASSISTANT",
                "text": text,
                "agent": self.agent.alias,
                "stream": stream,
            },
        )

    async def send_error_msg(self, error: Exception) -> TaskLogMessage:
        """
        Send an error message to the user.
        """
        assert isinstance(error, Exception), error
        traceback_list = traceback.format_exception(
            type(error), error, error.__traceback__
        )
        traceback_string = "".join(traceback_list)
        error_type = type(error).__name__
        parent_id = self.parent_think_msg.id if self.parent_think_msg else None
        failure_msg = await TaskLogMessage.objects.acreate(
            task_id=self.task.id,
            parent_id=parent_id,
            role="assistant",
            content={
                "type": "EXECUTE_ERROR",
                "error_type": error_type,
                "text": str(error),
                "details": traceback_string,
            },
        )
        logger.error(
            f"@@@@ EXECUTE ERROR logged as id={failure_msg.id} message_id={parent_id} error_type={error_type}"
        )
        logger.error(f"@@@@ EXECUTE ERROR {failure_msg.content['text']}")
        logger.error(exception_to_string(error))
        return failure_msg

    @staticmethod
    def from_manager(run_manager: AsyncCallbackManagerForChainRun):
        """Helper method for finding the IxHandler in a run_manager."""
        ix_handlers = [
            handler
            for handler in run_manager.handlers
            if isinstance(handler, IxHandler)
        ]
        if len(ix_handlers) == 0:
            raise ValueError("Expected at least one IxHandler in run_manager")
        if len(ix_handlers) != 1:
            raise ValueError("Expected exactly one IxHandler in run_manager")
        return ix_handlers[0]
