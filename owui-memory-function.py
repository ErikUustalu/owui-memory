"""
title: Custom memory
author: Erik Uustalu
author_url:https://github.com/ErikUustalu
version: 0.1
requirements: requests
"""

from pydantic import BaseModel, Field
from typing import Optional

import requests


class Filter:
    class Valves(BaseModel):
        url: str = Field(default="", description="Webserver URL")
        pass

    def __init__(self):
        self.valves = self.Valves()
        pass

    def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        first_run = not any(
            message.get("role") == "system"
            and "[Memories Summary (Use tools to get all memories)]"
            in message.get("content", "")
            for message in messages
        )

        if first_run:
            response = requests.get(self.valves.url)

            body["messages"].insert(
                1,
                {
                    "role": "system",
                    "content": f"[Memories Summary (Use tools to get all memories)]\n{response.text}",
                },
            )

        return body
