"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.
    
    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """
    
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
    
    def build_system_prompt(self, skill_names: list[str] | None = None,
                            skill_override: str | None = None,
                            knowledge_dir: Path | None = None,
                            agent_id: str | None = None) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.

        Args:
            skill_names: Optional list of skills to include.
            skill_override: If set, load only this skill (for agent-bound channels).
            knowledge_dir: Optional path to employee knowledge directory.
            agent_id: If set, enter employee isolation mode — only this
                      employee's skill and knowledge are visible.

        Returns:
            Complete system prompt.
        """
        # Employee isolation mode: strict boundary, no cross-contamination
        if agent_id and skill_override:
            return self._build_employee_prompt(
                agent_id=agent_id,
                skill_name=skill_override,
                knowledge_dir=knowledge_dir,
            )

        # --- Normal (non-employee) mode below ---
        parts = []

        # Core identity
        parts.append(self._get_identity())

        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # Skills - skill_override takes priority
        if skill_override:
            override_content = self.skills.load_skills_for_context([skill_override])
            if override_content:
                parts.append(f"# Active Skills\n\n{override_content}")
        else:
            # Always-loaded skills: include full content
            always_skills = self.skills.get_always_skills()
            if always_skills:
                always_content = self.skills.load_skills_for_context(always_skills)
                if always_content:
                    parts.append(f"# Active Skills\n\n{always_content}")

        # Knowledge base index (progressive loading)
        if knowledge_dir and knowledge_dir.exists():
            from nanobot.agent.knowledge import KnowledgeStore
            kb = KnowledgeStore(knowledge_dir)
            index = kb.build_index()
            if index:
                parts.append(
                    "# Knowledge Base\n\n"
                    "以下是你的业务知识库文件。需要时用 read_file 工具读取全文。\n\n"
                    + index
                )

        # Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _build_employee_prompt(self, agent_id: str, skill_name: str,
                               knowledge_dir: Path | None = None) -> str:
        """Build a strictly isolated system prompt for a digital employee.

        In employee mode:
        - NO generic nanobot identity
        - NO shared bootstrap files (AGENTS.md, SOUL.md, etc.)
        - NO shared memory
        - NO other skills summary
        - ONLY this employee's skill + knowledge base
        """
        from datetime import datetime
        import time as _time

        parts = []

        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"
        workspace_path = str(self.workspace.expanduser().resolve())
        agent_dir = f"{workspace_path}/agent/{agent_id}"

        # 1. Employee identity — loaded from SKILL.md
        skill_content = self.skills.load_skills_for_context([skill_name])
        if skill_content:
            parts.append(skill_content)

        # 2. Minimal runtime context
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        parts.append(f"""## Runtime
Current Time: {now} ({tz})
System: {runtime}

## Workspace
- 工作目录: {agent_dir}
- 知识库: {agent_dir}/knowledge/
- 工作日志: {agent_dir}/log.md
- 工作笔记: {agent_dir}/notes.md""")

        # 3. Knowledge base index
        if knowledge_dir and knowledge_dir.exists():
            from nanobot.agent.knowledge import KnowledgeStore
            kb = KnowledgeStore(knowledge_dir)
            index = kb.build_index()
            if index:
                parts.append(
                    "## Knowledge Base\n\n"
                    "以下是你的业务知识库文件。需要时用 read_file 工具读取全文。\n\n"
                    + index
                )

        # 4. Hard constraints — prevent cross-contamination
        parts.append(f"""## 行为约束（严格遵守）

- 你是数字员工 #{agent_id}，只能基于上述身份和技能回答问题
- 你只能访问自己的工作目录 {agent_dir}/ 下的文件
- 你不知道也不应提及其他员工、其他技能或系统级配置
- 如果用户问到你职责范围之外的事情，礼貌说明这不在你的专业范围内
- 重要工作记录写入 {agent_dir}/log.md
- 重要笔记和洞察写入 {agent_dir}/notes.md
- 回复时直接输出文本，不要调用 message 工具""")

        return "\n\n---\n\n".join(parts)
    
    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        
        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now} ({tz})

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable)
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. When using tools, think step by step: what you know, what you need, and why you chose this tool.
When remembering something important, write to {workspace_path}/memory/MEMORY.md
To recall past events, grep {workspace_path}/memory/HISTORY.md"""
    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        skill_override: str | None = None,
        knowledge_dir: Path | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (telegram, feishu, etc.).
            chat_id: Current chat/user ID.
            skill_override: If set, load only this skill.
            knowledge_dir: Optional path to employee knowledge directory.
            agent_id: If set, enter employee isolation mode.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(
            skill_names, skill_override=skill_override,
            knowledge_dir=knowledge_dir, agent_id=agent_id,
        )
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Current message (with optional image attachments)
        user_content = self._build_user_content(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text
        
        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        
        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
            reasoning_content: Thinking output (Kimi, DeepSeek-R1, etc.).
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        
        if tool_calls:
            msg["tool_calls"] = tool_calls
        
        # Thinking models reject history without this
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        
        messages.append(msg)
        return messages
