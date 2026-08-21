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
from openai import AsyncOpenAI, BadRequestError
from datetime import datetime
from aiohttp import web

from default_system_prompts import DEFAULT_MEMORY_PROMPT, DEFAULT_SUMMARY_PROMPT

dotenv.load_dotenv()

OWUI_TOKEN = os.getenv("OWUI_TOKEN")
OWUI_URL = os.getenv("OWUI_URL")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 86400))
MEMORY_PROMPT = os.getenv("MEMORY_PROMPT", DEFAULT_MEMORY_PROMPT)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")
OPENAI_URL = os.getenv("OPENAI_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")
MAX_MEMORIES = int(os.getenv("MAX_MEMORIES", 200))
SUMMARY_PROMPT = os.getenv("SUMMARY_PROMPT", DEFAULT_SUMMARY_PROMPT)
IGNORE_CHATS_INCL = os.getenv("IGNORE_CHATS_INCL", "[TR]")

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class Ai():
  def __init__(self, thinking=True, retry_attempts=5, retry_delay=15):
    self.thinking = thinking
    self.retry_attempts = retry_attempts
    self.retry_delay = retry_delay
    if OPENAI_URL:
      self.client = AsyncOpenAI(base_url=OPENAI_URL, api_key=OPENAI_API_KEY)
    else:
      self.client = genai.Client(api_key=GEMINI_API_KEY)

  async def generate(self, prompt):
    if OPENAI_URL:
      if self.thinking:
        reasoning = "high"
      else:
        reasoning = "none"

      for i in range(self.retry_attempts):
        try:
          response = await self.client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            reasoning_effort=reasoning
          )
          return response.choices[0].message.content
        except BadRequestError as e:
          logger.warning(f"Generation failed. Trying without reasoning")
          try:
            response = await self.client.chat.completions.create(
              model=OPENAI_MODEL,
              messages=[{"role": "user", "content": prompt}],
            )
            self.thinking = False
            return response.choices[0].message.content
          except Exception as e:
            logger.warning(f"AI generation failed (attempt {i+1}/{self.retry_attempts}): {e}")
            if i < self.retry_attempts - 1:
              logger.warning(f"Trying again in {self.retry_delay}s...")
              await asyncio.sleep(self.retry_delay)
            else:
              raise
        except Exception as e:
          logger.warning(f"AI generation failed (attempt {i+1}/{self.retry_attempts}): {e}")
          if i < self.retry_attempts - 1:
            logger.warning(f"Trying again in {self.retry_delay}s...")
            await asyncio.sleep(self.retry_delay)
          else:
            raise

    else:
      if self.thinking:
        aiconfig = types.GenerateContentConfig(
          thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
        )
      else:
        aiconfig = None

      for i in range(self.retry_attempts):
        try:
          response = await self.client.aio.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=aiconfig)
          return response.text
        except Exception as e:
          logger.warning(f"AI generation failed (attempt {i+1}/{self.retry_attempts}): {e}")
          if i < self.retry_attempts - 1:
            logger.warning(f"Trying again in {self.retry_delay}s...")
            await asyncio.sleep(self.retry_delay)
          else:
            raise

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
      await asyncio.sleep(UPDATE_INTERVAL)
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
  if not OWUI_TOKEN or not OWUI_URL:
    logger.error("Open WebUI credentials missing")
    return
  elif not (GEMINI_API_KEY or (OPENAI_API_KEY and OPENAI_URL)):
    logger.error("AI API credentials missing")
    return

  app = web.Application()
  app.router.add_get("/", handle)
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, '0.0.0.0', 8080)

  await asyncio.gather(site.start(), update_memories())

if __name__ == "__main__":
  asyncio.run(main())
