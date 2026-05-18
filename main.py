import os
import dotenv
import asyncio
import time
import re
import json
import logging

from owui_client import OpenWebUI
from owui_client.models.memories import AddMemoryForm
from google import genai
from google.genai import types
from datetime import datetime
from aiohttp import web

DEFAULT_MEMORY_PROMPT = """
You are a memory manager. You will receive:
1. EXISTING MEMORIES — a numbered list of currently saved memories, each with an ID
2. RECENT CONVERSATIONS — text from the past 24 hours

Your job is to output a JSON object with two keys: "create" and "delete".

Rules for "create":
- Add memories for explicitly stated facts AND strongly implied ones
- Infer interests, skills, and preferences from BEHAVIOR, not just direct statements:
    - If the user asks many questions about a topic, they are interested in it
    - If the user demonstrates knowledge while talking, they know it
    - If the user repeatedly does something a certain way, it's their preference
    - If a topic comes up across multiple conversations, it matters to them
- Think about what a smart human observer would conclude after reading all the conversations
- Each memory is a single self-contained sentence starting with "User"
- Be specific and concrete — prefer "User is a K-pop fan who follows BLACKPINK and Stray Kids" over "User likes music"
- Capture style/tone signals too: how the user writes, what they find annoying, how detailed they want answers

Rules for "delete":
- Include the ID of any existing memory directly contradicted by new information
- Include the ID of any existing memory superseded by an update (you will also be creating a replacement)
- Do NOT delete memories just because they weren't mentioned recently

Do NOT create memories for:
- One-off questions that show no clear pattern (looking something up ≠ being interested in it)
- Things that are temporary or session-specific
- Anything you'd need to stretch or guess to conclude

Return ONLY raw JSON, no explanation, no markdown fences.

Example output:
{
  "create": [
    "User is deeply into K-pop, frequently discussing groups, comebacks, and lyrics",
    "User writes in a casual, clipped style and prefers responses that match that tone",
    "User knows Python well based on the technical depth of their coding questions"
  ],
  "delete": [3, 7]
}

If nothing to create, return "create": [].
If nothing to delete, return "delete": [].
"""

DEFAULT_SUMMARY_PROMPT = """
You are a personal context summarizer. You will receive a list of memories about a user. Your job is to write a dense, well-organized summary of the most important information that an AI assistant would need to know to interact with this person effectively.

Structure the summary in natural prose paragraphs, not bullet points. Group related information together logically. Prioritize:
1. Who the user is (background, location, occupation, life situation)
2. What they're actively working on right now
3. Their technical stack and environment
4. Interests and hobbies
5. Communication style and preferences

Rules:
- Target length: ~1024 tokens (roughly 750 words). Do not go significantly over or under.
- Write in third person ("The user...", "They...")
- Be dense and specific — every sentence should carry real information, no filler
- If there are many memories, prioritize recurring themes and recent/active things over old or one-off facts
- Do not invent anything not present in the memories
- Do not output anything except the summary — no headers, no intro line, no "Here is a summary:"
"""

dotenv.load_dotenv()

OWUI_TOKEN = os.getenv("OWUI_TOKEN")
OWUI_URL = os.getenv("OWUI_URL")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 86400))
MEMORY_PROMPT = os.getenv("MEMORY_PROMPT", DEFAULT_MEMORY_PROMPT)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")
MAX_MEMORIES = int(os.getenv("MAX_MEMORIES", 200))
SUMMARY_PROMPT = os.getenv("SUMMARY_PROMPT", DEFAULT_SUMMARY_PROMPT)

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

async def update_memories():
  while True:
    logger.info("Starting memory update...")
    owui = OpenWebUI(api_url=OWUI_URL, api_key=OWUI_TOKEN)

    logger.info("Getting chats...")
    chats_raw = await owui.chats.get_list()
    chats = []
    now = time.time()
    
    for chat in chats_raw:
      if now - chat.updated_at <= UPDATE_INTERVAL:
        chats.append(chat)

    if not chats:
      logger.info("No recent chats. Skipping memory update.")
      return

    chatstext = ""

    for chat in chats:
      chatstext += f"\n\n\n{chat.title}:"
      content = await owui.chats.get(chat.id)
      messages = content.chat["history"]["messages"]

      for message in messages.values():
        role = message["role"]
        content = message["content"]

        if role == "assistant":
          content = re.sub(r"<details[^>]*>.*?</details>", "", content, flags=re.DOTALL)
          content = re.sub(r"```[\s\S]*?```", lambda m: m.group(), content)
          content = content.strip()

        chatstext += f"\n{role}: {content}"

    logger.info("Getting existing memories...")
    memories = await owui.memories.get_memories()
    memoriestext = ""
    memory_id_map = {}

    i = 0
    for memory in memories:
      created_at = datetime.fromtimestamp(memory.created_at).strftime("%Y-%m-%d")
      memory_id_map[i] = memory.id
      memoriestext += f"\nid: {i}, created_at: {created_at}, content: {memory.content}"
      i += 1

    logger.info("Generating memory updates")
    prompt = f"{MEMORY_PROMPT}\n\nMEMORIES:\n{memoriestext}\n\nCONVERSATIONS:\n{chatstext}\n\n"
    aiconfig = types.GenerateContentConfig(
      thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
    )

    ai = genai.Client(api_key=GEMINI_API_KEY)
    response = await ai.aio.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=aiconfig)
    results = json.loads(response.text)

    for creation in results["create"]:
      await owui.memories.add_memory(AddMemoryForm(content=creation))
      logger.info(f'Created memory: "{creation}"')
    
    for deletion in results["delete"]:
      memory_id = memory_id_map[deletion]
      await owui.memories.delete_memory_by_id(memory_id)
      logger.info(f'Deleted memory with ID: {memory_id}')

    logger.info("Finished updating memories")

    logger.info("Checking memory count...")
    memories = await owui.memories.get_memories()
    if len(memories) > MAX_MEMORIES:
      logger.info("Deleting old memories...")
      for memory in memories[:len(memories)-MAX_MEMORIES]:
        await owui.memories.delete_memory_by_id(memory.id)
        logger.info(f'Deleted old memory with ID: {memory.id}')

    logger.info("Finished checking memory count")

    logger.info("Generating new summary...")
    memories = await owui.memories.get_memories()
    memoriestext = ""

    for memory in memories:
      created_at = datetime.fromtimestamp(memory.created_at).strftime("%Y-%m-%d")
      memoriestext += f"\ncreated_at: {created_at}, content: {memory.content}"

    prompt = f"{SUMMARY_PROMPT}\n\nMEMORIES:\n{memoriestext}\n\n"
    aiconfig = types.GenerateContentConfig(
      thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
    )

    ai = genai.Client(api_key=GEMINI_API_KEY)
    response = await ai.aio.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=aiconfig)
    summary = response.text
    summary_file = open("data/summary.txt", "w")
    summary_file.write(summary)
    summary_file.close()
    logger.info("Finished generating new summary")
    logger.info("Update complete")

    logger.info(f"Next run in {UPDATE_INTERVAL} seconds...")
    await asyncio.sleep(UPDATE_INTERVAL)

async def handle(request):
  with open("data/summary.txt", "r") as f:
    summary = f.read()
  return web.Response(text=summary)

async def main():
  app = web.Application()
  app.router.add_get("/", handle)
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, '0.0.0.0', 8080)

  await asyncio.gather(site.start(), update_memories())

if __name__ == "__main__":
  asyncio.run(main())