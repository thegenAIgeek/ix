import pytest
from uuid import uuid4, UUID

from httpx import AsyncClient

from ix.agents.models import Agent
from ix.chains.management.commands.create_ix_v2 import IX_AGENT_V2
from ix.chat.models import Chat
from ix.server.fast_api import app
from ix.task_log.models import TaskLogMessage, Task
from ix.task_log.tests.fake import (
    afake_agent,
    afake_chat,
    afake_artifact,
    afake_user,
    afake_system,
    afake_task,
)

CHAT_ID_1 = uuid4()
CHAT_ID_2 = uuid4()


@pytest.mark.django_db
class TestChat:
    async def test_create_chat(self, anode_types, aix_agent):
        """Chat with default lead agent"""
        await afake_user()
        data = {"name": "New Chat", "autonomous": False}

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post("/chats/", json=data)

        assert response.status_code == 200, response.content
        result = response.json()

        # Check that we created the chat
        assert result["name"] == "New Chat"
        assert result["lead_id"] == str(IX_AGENT_V2)
        assert result["autonomous"] is False
        assert await Chat.objects.filter(id=result["id"]).aexists()

    async def test_create_chat_with_custom_lead(self, anode_types, aix_agent):
        """Chat with custom lead agent"""
        await afake_user()
        agent = await afake_agent()
        data = {"name": "New Chat", "autonomous": False, "lead_id": str(agent.id)}

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post("/chats/", json=data)

        assert response.status_code == 200, response.content
        result = response.json()

        # Check that we created the chat
        assert result["name"] == "New Chat"
        assert result["lead_id"] == str(agent.id)
        assert result["autonomous"] is False
        assert await Chat.objects.filter(id=result["id"]).aexists()

    async def test_get_chat(self, anode_types):
        chat = await afake_chat(name="Chat 1")
        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get(f"/chats/{chat.id}")

        assert response.status_code == 200, response.content
        result = response.json()

        # Check if we got the correct chat details
        assert result["id"] == str(chat.id)
        assert result["name"] == chat.name

    async def test_get_chat_not_found(self):
        non_existent_chat_id = uuid4()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get(f"/chats/{non_existent_chat_id}")

        assert response.status_code == 404
        result = response.json()
        assert result["detail"] == "Chat not found"

    async def test_get_chats(self, anode_types):
        await Chat.objects.all().adelete()
        chat_1 = await afake_chat(name="Chat 1", id=CHAT_ID_1)
        chat_2 = await afake_chat(name="Chat 2", id=CHAT_ID_2)

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get("/chats/")

        assert response.status_code == 200, response.content
        result = response.json()

        # Check if we got a list of chats
        assert len(result["objects"]) == 2
        chat_ids = [chat["id"] for chat in result["objects"]]
        assert str(chat_1.id) in chat_ids
        assert str(chat_2.id) in chat_ids

        # pagination sanity check
        assert result["count"] == 2
        assert result["pages"] == 1
        assert result["page_number"] == 1
        assert result["has_next"] is False
        assert result["has_previous"] is False

    @pytest.mark.parametrize(
        "search_term, expected_ids",
        [
            ["mock", [CHAT_ID_1, CHAT_ID_2]],
            ["mock Chat 1", [CHAT_ID_1]],
            ["mock Chat 2", [CHAT_ID_2]],
            ["matches none", []],
        ],
    )
    async def test_search_chats(self, anode_types, search_term, expected_ids):
        await Chat.objects.all().adelete()
        await afake_chat(name="mock Chat 1", id=CHAT_ID_1)
        await afake_chat(name="mock Chat 2", id=CHAT_ID_2)

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get(f"/chats/?search={search_term}")

        assert response.status_code == 200, response.content
        result = response.json()
        assert len(result["objects"]) == len(expected_ids)
        assert {UUID(chat["id"]) for chat in result["objects"]} == set(expected_ids)

    async def test_update_chat(self):
        chat = await afake_chat()
        data = {"name": "New Chat", "lead_id": str(chat.lead_id), "autonomous": False}

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{chat.id}", json=data)

        assert response.status_code == 200, response.content
        result = response.json()

        # Check that we updated the chat
        assert result["id"] == str(chat.id)
        assert result["name"] == "New Chat"
        assert result["lead_id"] == str(chat.lead_id)
        assert result["autonomous"] is False

    async def test_update_chat_not_found(self):
        non_existent_chat_id = uuid4()

        # Prepare the data for the API request
        data = {"name": "New Chat", "lead_id": str(uuid4()), "autonomous": False}

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{non_existent_chat_id}", json=data)

        assert response.status_code == 404, response.content
        result = response.json()
        assert result["detail"] == "Chat not found"

    async def test_delete_chat(self, anode_types):
        chat = await afake_chat()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.delete(f"/chats/{chat.id}")

        # Assert the status code and the response
        assert response.status_code == 200, response.content
        result = response.json()
        assert result == {"id": str(chat.id)}

        # Ensure the chat is actually deleted
        assert not await Chat.objects.filter(id=chat.id).aexists()

    async def test_delete_chat_not_found(self, anode_types):
        non_existent_chat_id = uuid4()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.delete(f"/chats/{non_existent_chat_id}")

        # Assert the status code and the response
        assert response.status_code == 404, response.content
        result = response.json()
        assert result["detail"] == "Chat not found"


@pytest.mark.django_db
class TestChatAgents:
    async def test_add_agent_to_chat(self, anode_types):
        chat = await afake_chat()
        agent = await afake_agent()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{chat.id}/agents/{agent.id}")

        assert response.status_code == 200
        result = response.json()

        # verify that the agent is added
        assert result == dict(
            chat_id=str(chat.id),
            agent_id=str(agent.id),
        )
        assert await chat.agents.filter(id=agent.id).aexists()

    async def test_add_existing_lead_agent_to_chat(self):
        chat = await afake_chat()
        agent = chat.lead

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{chat.id}/agents/{agent.id}")

        assert response.status_code == 200
        result = response.json()

        # Check that the response returns the same chat as no changes were made
        assert result["chat_id"] == str(chat.id)
        assert result["agent_id"] is None

    async def test_add_existing_agent_to_chat_agents(self):
        chat = await afake_chat()
        agent = await afake_agent()
        await chat.agents.aadd(agent)

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{chat.id}/agents/{agent.id}")

        assert response.status_code == 200
        result = response.json()

        # Check that the response returns the same chat as no changes were made
        assert result["chat_id"] == str(chat.id)
        assert result["agent_id"] is None

    async def test_add_agent_chat_not_found(self):
        non_existent_chat_id = uuid4()
        agent = await afake_agent()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{non_existent_chat_id}/agents/{agent.id}")

        assert response.status_code == 404, response.content
        result = response.json()
        assert result["detail"] == "Chat does not exist."

    async def test_add_agent_agent_not_found(self):
        chat = await afake_chat()
        non_existent_agent_id = uuid4()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.put(f"/chats/{chat.id}/agents/{non_existent_agent_id}")

        assert response.status_code == 404, response.content
        result = response.json()
        assert result["detail"] == "Agent does not exist."

    async def test_remove_agent_from_chat(self, anode_types):
        chat = await afake_chat()
        agent = await afake_agent()

        # add agent to chat first
        await chat.agents.aadd(agent)

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.delete(f"/chats/{chat.id}/agents/{agent.id}")

        assert response.status_code == 200
        result = response.json()

        # verify that the agent is removed
        assert result == dict(
            chat_id=str(chat.id),
            agent_id=str(agent.id),
        )
        assert not await chat.agents.filter(id=agent.id).aexists()

    async def test_remove_agent_chat_not_found(self):
        non_existent_chat_id = uuid4()
        agent = await afake_agent()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.delete(
                f"/chats/{non_existent_chat_id}/agents/{agent.id}"
            )

        assert response.status_code == 404
        result = response.json()
        assert result["detail"] == "Chat does not exist."

    async def test_remove_agent_agent_not_found(self):
        chat = await afake_chat()
        non_existent_agent_id = uuid4()

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.delete(
                f"/chats/{chat.id}/agents/{non_existent_agent_id}"
            )

        assert response.status_code == 404
        result = response.json()
        assert result["detail"] == "Agent does not exist."


@pytest.mark.django_db
class TestChatGraph:
    async def test_get_chat_graph(self, anode_types):
        chat = await afake_chat()
        fake_artifact = await afake_artifact(task_id=chat.task_id)
        agent = await afake_agent()
        await chat.agents.aadd(agent)

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get(f"/chats/{chat.id}/graph")

        assert response.status_code == 200
        result = response.json()

        # verify the chat graph response
        assert result["chat"]["id"] == str(chat.id)
        assert result["chat"]["name"] == chat.name
        assert result["lead"]["id"] == str(chat.lead.id)
        assert len(result["agents"]) == 1
        assert result["agents"][0]["id"] == str(agent.id)
        assert len(result["plans"]) == 0
        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0]["id"] == str(fake_artifact.id)


@pytest.fixture()
def mock_start_agent_loop(mocker):
    yield mocker.patch("ix.api.chats.endpoints.start_agent_loop")


@pytest.mark.django_db
class TestChatMessage:
    async def test_send_message(self, anode_types, mock_start_agent_loop):
        chat = await afake_chat()
        lead = await Agent.objects.aget(id=chat.lead_id)
        text = "Test message"

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post(f"/chats/{chat.id}/messages", json={"text": text})

        assert response.status_code == 200, response.content
        result = response.json()

        assert result["content"]["type"] == "FEEDBACK"
        assert result["content"]["feedback"] == text
        message = await TaskLogMessage.objects.aget(id=result["id"])
        assert message.content["type"] == "FEEDBACK"
        assert message.content["feedback"] == text

        # Agent isn't being set on message for now, this may be deprecated in the future
        assert result["agent_id"] is None
        assert message.agent_id is None

        call_args = mock_start_agent_loop.delay.call_args_list
        print(call_args)
        mock_start_agent_loop.delay.assert_called_once_with(
            str(chat.task_id),
            str(lead.chain_id),
            inputs={"user_input": text, "chat_id": str(chat.id), "artifact_keys": []},
        )

    async def test_send_message_with_artifact(self, anode_types, mock_start_agent_loop):
        chat = await afake_chat()
        lead = await Agent.objects.aget(id=chat.lead_id)
        text = "Test message with {test_artifact}"

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post(f"/chats/{chat.id}/messages", json={"text": text})

        assert response.status_code == 200, response.content
        result = response.json()

        print(result)
        assert result["content"]["type"] == "FEEDBACK"
        assert result["content"]["feedback"] == text
        assert result["agent_id"] is None
        message = await TaskLogMessage.objects.aget(id=result["id"])
        assert message.agent_id is None
        assert message.content["type"] == "FEEDBACK"
        assert message.content["feedback"] == text

        call_args = mock_start_agent_loop.delay.call_args_list
        print(call_args)
        mock_start_agent_loop.delay.assert_called_once_with(
            str(chat.task_id),
            str(lead.chain_id),
            inputs={
                "user_input": text,
                "chat_id": str(chat.id),
                "artifact_keys": ["test_artifact"],
            },
        )

    async def test_send_message_to_agent(self, anode_types, mock_start_agent_loop):
        chat = await afake_chat()
        agent = await afake_agent(alias="a_fake_agent")
        await chat.agents.aadd(agent)
        text = "@a_fake_agent Test message to agent"

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post(f"/chats/{chat.id}/messages", json={"text": text})

        assert response.status_code == 200, response.content
        result = response.json()

        assert result["content"]["type"] == "FEEDBACK"
        assert result["content"]["feedback"] == text
        assert result["agent_id"] is None
        message = await TaskLogMessage.objects.aget(id=result["id"])
        assert message.agent_id is None
        assert message.content["type"] == "FEEDBACK"
        assert message.content["feedback"] == text

        # verify that the agent is started
        subtask = await Task.objects.aget(parent_id=chat.task_id)
        mock_start_agent_loop.delay.assert_called_once_with(
            str(subtask.id),
            str(agent.chain_id),
            inputs={"user_input": text, "chat_id": str(chat.id), "artifact_keys": []},
        )

    async def test_get_messages(self, anode_types):
        chat = await afake_chat()
        task = await Task.objects.aget(id=chat.task_id)
        subtask = await afake_task(parent=task)
        msg1 = await afake_system("test1", task=task)
        msg2 = await afake_system("test2", task=subtask)

        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.get(f"/chats/{chat.id}/messages")

        assert response.status_code == 200, response.content
        result = response.json()
        msgs = result["objects"]

        assert len(msgs) == 3
        assert msgs[0]["content"]["type"] == "FEEDBACK"
        assert msgs[1]["id"] == str(msg1.id)
        assert msgs[1]["content"]["type"] == "SYSTEM"
        assert msgs[1]["content"]["message"] == "test1"
        assert msgs[2]["id"] == str(msg2.id)
        assert msgs[2]["content"]["type"] == "SYSTEM"
        assert msgs[2]["content"]["message"] == "test2"
