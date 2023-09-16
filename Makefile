DOCKER_COMPOSE=docker-compose.yml
DOCKER_REGISTRY=ghcr.io

# check for md5sum or md5 for hashing
HASHER := $(shell command -v md5sum 2> /dev/null)
ifndef HASHER
    HASHER := md5 -r
endif

# APP IMAGE
DOCKERFILE=Dockerfile
DOCKERFILE_TARGET=app
DOCKER_REPOSITORY=${DOCKER_REGISTRY}/kreneskyp/ix/sandbox
HASH_FILES=requirements*.txt package.json Dockerfile
IMAGE_TAG=$(shell cat $(HASH_FILES) | ${HASHER} | cut -d ' ' -f 1)
IMAGE_URL=$(DOCKER_REPOSITORY):$(IMAGE_TAG)
IMAGE_SENTINEL=.sentinel/image

# NODEJS / FRONTEND BUILDER IMAGE
DOCKERFILE_NODEJS=Dockerfile
DOCKERFILE_TARGET_NODEJS=nodejs
DOCKER_REPOSITORY_NODEJS=${DOCKER_REGISTRY}/kreneskyp/ix/nodejs
HASH_FILES_NODEJS=Dockerfile package.json babel.config.js webpack.config.js relay.config.js
IMAGE_TAG_NODEJS=$(shell cat $(HASH_FILES_NODEJS) | ${HASHER} | cut -d ' ' -f 1)
IMAGE_URL_NODEJS=$(DOCKER_REPOSITORY_NODEJS):$(IMAGE_TAG_NODEJS)
IMAGE_SENTINEL_NODEJS=.sentinel/image.nodejs

# PSQL IMAGE
DOCKERFILE_PSQL=psql.Dockerfile
DOCKER_REPOSITORY_PSQL=${DOCKER_REGISTRY}/kreneskyp/ix/postgres-pg-vector
HASH_FILES_PSQL=psql.Dockerfile
IMAGE_TAG_PSQL=$(shell cat $(HASH_FILES_PSQL) | ${HASHER} | cut -d ' ' -f 1)
IMAGE_URL_PSQL=$(DOCKER_REPOSITORY_PSQL):$(IMAGE_TAG_PSQL)
IMAGE_SENTINEL_PSQL=.sentinel/image.psql

DOCKER_COMPOSE_RUN=docker-compose run --rm web
DOCKER_COMPOSE_RUN_WITH_PORT=docker-compose run -p 8000:8000 --rm web
DOCKER_COMPOSE_RUN_NODEJS=docker-compose run --rm nodejs


# set to skip build, primarily used by github workflows to skip builds when image is cached
NO_IMAGE_BUILD?=0

.PHONY: image-name
image-name:
	@echo ${DOCKER_REPOSITORY}

.PHONY: image-tag
image-tag:
	@echo ${IMAGE_TAG}


.PHONY: image-url
image-url:
	@echo ${IMAGE_URL}

.PHONY: image-url-nodejs
image-url-nodejs:
	@echo ${IMAGE_URL_NODEJS}

# build existence check
.sentinel:
	mkdir -p .sentinel

# Set LANGCHAIN_DEV to 1 to enable dev mode in docker build
# local copy of langchain should be checked out to ix/langchain.
# (docker desktop on windows doesn't support shares outside the project)
LANGCHAIN_DEV ?=
DOCKER_BUILD_ARGS = $(if ${LANGCHAIN_DEV},--build-arg LANGCHAIN_DEV=${LANGCHAIN_DEV},)

# inner build target for sandbox image
${IMAGE_SENTINEL}: .sentinel $(HASH_FILES)
ifneq (${NO_IMAGE_BUILD}, 1)
	echo building SANDBOX ${IMAGE_URL}
	docker build -t ${IMAGE_URL} -f ${DOCKERFILE} --target ${DOCKERFILE_TARGET} ${DOCKER_BUILD_ARGS} .
	docker tag ${IMAGE_URL} ${DOCKER_REPOSITORY}:latest
	touch $@
endif

# inner build target for nodejs frontend builder image
${IMAGE_SENTINEL_NODEJS}: .sentinel $(HASH_FILES_NODEJS)
ifneq (${NO_IMAGE_BUILD}, 1)
	echo building NODEJS ${IMAGE_URL_NODEJS}
	docker build -t ${IMAGE_URL_NODEJS} -f $(DOCKERFILE_NODEJS) --target ${DOCKERFILE_TARGET_NODEJS} .
	docker tag ${IMAGE_URL_NODEJS} ${DOCKER_REPOSITORY_NODEJS}:latest
	touch $@
endif

# inner build target for postgres image
${IMAGE_SENTINEL_PSQL}: .sentinel $(HASH_FILES_PSQL)
ifneq (${NO_IMAGE_BUILD}, 1)
	echo building POSTGRES ${IMAGE_URL_PSQL}
	docker build -t ${IMAGE_URL_PSQL} -f $(DOCKERFILE_PSQL)  .
	docker tag ${IMAGE_URL_PSQL} ${DOCKER_REPOSITORY_PSQL}:latest
	touch $@
endif

# setup target for docker-compose, add deps here to apply to all compose sessions
.PHONY: compose
compose: image

# =========================================================
# Build
# =========================================================

# dev setup - runs all initial setup steps in one go
.PHONY: dev_setup
dev_setup: image frontend migrate dev_fixtures

.compiled-static:
	# create empty dir so it is always present for docker build. Local & test
	# builds do not use it, but it the dir needs to exist for the build to work
	# Local and test builds use a docker volume to mount the file in. Only
	# release builds add the compiled files to the image
	mkdir -p .compiled-static

# build image
.PHONY: image
image: .compiled-static ${IMAGE_SENTINEL} ${IMAGE_SENTINEL_PSQL}

# nodejs / frontend builder image
.PHONY: nodejs
nodejs: ${IMAGE_SENTINEL_NODEJS}


# full frontend build
.PHONY: frontend
frontend: nodejs graphene_to_graphql compile_relay webpack

# install npm packages
.PHONY: npm_install
npm_install: nodejs package.json
	${DOCKER_COMPOSE_RUN_NODEJS} npm install

# compile javascript
.PHONY: webpack
webpack: nodejs
	${DOCKER_COMPOSE_RUN_NODEJS} webpack --progress

# compile javascript in watcher mode
.PHONY: webpack-watch
webpack-watch: nodejs
	${DOCKER_COMPOSE_RUN_NODEJS} webpack --progress --watch

# compile graphene graphql classes into schema.graphql for javascript
.PHONY: graphene_to_graphql
graphene_to_graphql: compose
	${DOCKER_COMPOSE_RUN} ./manage.py graphql_schema --out ./frontend/schema.graphql

# compile javascript
.PHONY: compile_relay
compile_relay: nodejs
	${DOCKER_COMPOSE_RUN_NODEJS} npm run relay


# =========================================================
# Run
# =========================================================


.PHONY: cluster
cluster: compose
	docker-compose up -d web nginx worker


.PHONY: up
up: cluster


.PHONY: down
down: compose
	docker-compose down


# run backend and frontend. This starts uvicorn for asgi+websockers
# and nginx to serve static files
.PHONY: server
server: cluster
	@docker-compose logs -f --tail=10 web nginx


# run django debug server, backup in case nginx ever breaks
.PHONY: runserver
runserver: compose
	${DOCKER_COMPOSE_RUN_WITH_PORT} ./manage.py runserver 0.0.0.0:8000

# run worker
.PHONY: worker
worker: compose
	@docker-compose logs -f --tail=10 worker

# reset worker (for code refresh)
.PHONY: worker-reset
worker-reset: compose
	@echo stopping workers...
	@docker-compose up -d --scale worker=0
	@echo restarting worker...
	@docker-compose up -d --scale worker=1


# =========================================================
# Shells
# =========================================================

.PHONY: bash
bash: compose
	${DOCKER_COMPOSE_RUN} /bin/bash

.PHONY: shell
shell: compose
	${DOCKER_COMPOSE_RUN} ./manage.py shell_plus

# =========================================================
# Dev tools
# =========================================================

# shortcut to run django migrations
.PHONY: migrate
migrate: compose
	${DOCKER_COMPOSE_RUN} ./manage.py migrate

# shortcut to generate django migrations
.PHONY: migrations
migrations: compose
	${DOCKER_COMPOSE_RUN} ./manage.py makemigrations

# load initial data needed for dev environment
.PHONY: dev_fixtures
dev_fixtures: compose
	${DOCKER_COMPOSE_RUN} ./manage.py loaddata fake_user

	# load component NodeTypes
	${DOCKER_COMPOSE_RUN} ./manage.py loaddata node_types

 	# initial agents + chains
	${DOCKER_COMPOSE_RUN} ./manage.py loaddata ix_v2 code_v2 pirate_v1 wikipedia_v1 klarna_v1 bot_smith_v1


# Generate fixture for NodeTypes defined in python fixtures.
# This converts all NodeTypes present in the database into a
# Django fixture required for unit tests.
#
# This will import_langchain and then export both
# new and existing types from the table.
.PHONY: node_types_fixture
node_types_fixture: compose
	${DOCKER_COMPOSE_RUN} ./manage.py import_langchain
	${DOCKER_COMPOSE_RUN} ./manage.py dumpdata chains.NodeType --indent 2 > ix/chains/fixtures/node_types.json

# =========================================================
# Testing
# =========================================================

.PHONY: test
test: compose pytest

.PHONY: lint
lint: compose flake8 black-check

.PHONY: format
format: black isort

.PHONY: black
black: compose
	${DOCKER_COMPOSE_RUN} black .

.PHONY: black-check
black-check: compose
	${DOCKER_COMPOSE_RUN} black --check .

.PHONY: flake8
flake8: compose
	${DOCKER_COMPOSE_RUN} flake8 .

.PHONY: isort
isort: compose
	${DOCKER_COMPOSE_RUN} isort .

.PHONY: pytest
pytest: compose
	${DOCKER_COMPOSE_RUN} pytest ix

.PHONY: pyright
pyright: compose
	${DOCKER_COMPOSE_RUN} pyright

.PHONY: prettier
prettier: nodejs
	${DOCKER_COMPOSE_RUN_NODEJS} prettier -w frontend

# =========================================================
# Cleanup
# =========================================================

.PHONY: clean
clean:
	rm -rf .sentinel