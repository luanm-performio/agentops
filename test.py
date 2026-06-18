import anyio
from claude_agent_sdk import query

from claude_agent_sdk import query, ClaudeAgentOptions

async def main():
    async for message in query(prompt="What is 2 + 2?", options=ClaudeAgentOptions(
        # Explicitly allow user (~/.claude) and project (./.claude) folders
        setting_sources=["user", "project"], 
        skills="all",  # Ensure this is not empty
    )):
        print(message)

anyio.run(main)
