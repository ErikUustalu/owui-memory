# OWUI Memory

A custom memory manager for Open Webui

> [!NOTE]
> The default system prompts were created with AI assistance

## Features
**Automatic memory generation:** Runs at set intervals and automatically generates new memories based on new chats
**Deduplication:** The model checks if the memory already exists before creating one
**Conflict resolving:** If something the user says in a chat conflicts with an existing memory, the memory will get deleted and replaced with a new one including the new correct info
**Summary generation:** Generates a summary of the memories that gets injected to the Open Webui prompt

> [!NOTE]
> The summary generation feature won't receive updates since the automatic memory injection of Open Webui 10.0 does the same thing better so I don't use the summary generation anymore

## Quick Setup
### Docker compose
1. Copy the example [docker-compose.yaml](docker-compose.yaml)
2. Create a data folder
   ```
   mkdir memory_data
   ```
3. Start the container
   ```
   docker compose up -d
   ```
