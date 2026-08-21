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

**THINK STEP BY STEP BEFORE OUTPUTTING JSON:**

**Step 1 — Scan conversations for new facts:**
- Extract explicitly stated facts about the user (preferences, projects, life situation, habits, opinions)
- Infer interests, skills, and preferences from BEHAVIOR:
  - If the user asks many questions about a topic → they're interested in it
  - If they demonstrate knowledge while talking → they know it
  - If they repeatedly do something a certain way → that's their preference
  - If a topic comes up across multiple conversations → it matters to them
- Each memory is a SINGLE atomic fact — one piece of information per memory
- Start each memory with "User"
- Be specific and concrete: prefer "User is a K-pop fan who follows BLACKPINK" over "User likes music"
- Break compound facts apart: "User owns a Škoda Karoq" and "User lives with 4 chihuahuas" are two separate memories, never one

**Step 2 — Split existing compound memories into atomic facts:**
- Scan ALL existing memories. If any memory contains MULTIPLE distinct facts joined by semicolons, commas, or "and" — plan to DELETE it and CREATE one memory per individual fact
- Example: "User likes pizza and lives in Estonia and owns a dog" → DELETE it, CREATE "User likes pizza", "User lives in Estonia", "User owns a dog"
- This is the HIGHEST PRIORITY cleanup task. Compound memories defeat the purpose of a searchable memory system

**Step 3 — Check each new fact against existing memories:**
- If an existing memory ALREADY COVERS this fact → skip creation entirely
- If a new fact contradicts an existing memory → plan to DELETE the old one and CREATE the corrected version
- If a new fact is MORE SPECIFIC than an existing one → DELETE the vague one, CREATE the specific one

**Step 4 — Deduplicate:**
- If two or more existing memories say the same thing in different words → DELETE all but the best-worded one (or DELETE all and CREATE one consolidated version ONLY if they truly describe the same single fact)

**Rules for "create":**
- ONE fact per memory. Never join multiple facts with semicolons, commas, lists, or "and"
- Each memory starts with "User"
- Be concrete and specific
- A single memory should be a short, searchable sentence — not a paragraph

**Rules for "delete":**
- Include IDs of compound memories being split into individual facts
- Include IDs of memories contradicted by new info
- Include IDs of vague memories replaced by more specific ones
- Include IDs of true duplicates
- Do NOT delete memories just because they weren't mentioned recently

**NEVER create memories for:**
- One-off questions that show no clear pattern
- Basic programming or technical concepts the user already knows
- Things that are temporary, session-specific, or situational venting
- Anything you'd need to stretch or guess to conclude

Return ONLY raw JSON, no explanation, no markdown fences.

Example output:
{
  "create": [
    "User is deeply into K-pop",
    "User frequently discusses groups, comebacks, and lyrics",
    "User writes in a casual, clipped style",
    "User prefers responses that match their casual tone",
    "User knows Python well"
  ],
  "delete": [3, 7]
}

If nothing to create, return "create": [].
If nothing to delete, return "delete": [].
"""

DEFAULT_SUMMARY_PROMPT = """
You are a personal context summarizer. You receive a list of memories about Erik. Write a dense, concise summary that an AI assistant needs to interact with him effectively.

Structure as natural prose paragraphs (no bullet points, no headings). Group related info logically.

Prioritize in this order:
1. Communication style, preferences, and personality — affects every response
2. Active projects and what he's currently working on
3. Technical environment (tools, stack, setup)
4. Who he is (background, age, location — keep brief)
5. Interests and hobbies (only if they frequently affect conversation)

Rules:
- Target length: ~200 tokens (~150 words). Hard limit: 500 tokens.
- Write in third person using "Erik" (not "the user")
- Every sentence must carry real information — zero filler
- Heavily favor recent/active information over static biographical facts
- Exclude: technical specs (voltages, models, versions), one-off facts, temporary setup details, conversational history, anything that won't change how the AI responds
- Do not invent anything not present in memories
- Output ONLY the summary text — no markdown, no headers, no "Here is a summary:", no trailing sentences
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
IGNORE_CHATS_INCL = os.getenv("IGNORE_CHATS_INCL", "[TR]")

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
      continue

    chatstext = ""

    for chat in chats:
      content = await owui.chats.get(chat.id)
      if IGNORE_CHATS_INCL in content:
        continue

      chatstext += f"\n\n\n{chat.title}:"
      messages = content.chat["history"]["messages"]

      for message in messages.values():
        try:
          role = message["role"]
          content = message["content"]
        except:
          continue

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

    for i in range(40):
      try:
        response = await ai.aio.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=aiconfig)
        break
      except Exception as e:
        logger.warning(f"Memory update generation failed (attempt {i+1}/40): {e}")
        logger.warning("Trying again in 15s...")
        await asyncio.sleep(15)

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

    for i in range(40):
      try:
        response = await ai.aio.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=aiconfig)
        break
      except Exception as e:
        logger.warning(f"Summary generation failed (attempt {i+1}/40): {e}")
        logger.warning("Trying again in 15s...")
        await asyncio.sleep(15)

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