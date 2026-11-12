"""简单数据处理算子。

样本格式（OpenAI 风格）：
{
  "tools": [{"type": "function", "function": {"name": "...", ...}}],   # 可选
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [{"id": "..", "type": "function", "function": {"name": "..", "arguments": "{...}"}}]},
    {"role": "tool", "tool_call_id": "..", "content": "..."}
  ]
}

每个算子：输入一条样本，返回处理后的样本。
"""
import json
import re


def rename_tools(sample, mapping):
    """工具名称替换。mapping 例：{"shell": "exec_command", "apply_patch": "edit_file"}"""
    # 1. 工具定义里的名字
    for tool in sample.get("tools", []):
        name = tool["function"]["name"]
        tool["function"]["name"] = mapping.get(name, name)

    for msg in sample["messages"]:
        # 2. assistant 发起的工具调用
        for call in msg.get("tool_calls", []):
            name = call["function"]["name"]
            call["function"]["name"] = mapping.get(name, name)
        # 3. tool 消息里的 name 字段（如果有）
        if "name" in msg:
            msg["name"] = mapping.get(msg["name"], msg["name"])
    return sample


def ensure_user_instruction(sample, default_text):
    """确认有用户指令：没有 user 消息就在开头补一条 default_text。"""
    has_user = any(m["role"] == "user" and m.get("content") for m in sample["messages"])
    if not has_user:
        sample["messages"].insert(0, {"role": "user", "content": default_text})
    return sample


def replace_text(sample, rules, use_regex=False):
    """字符匹配替换。rules 例：[("/Users/xxx", "/workspace"), ("Claude", "助手")]
    作用于每条消息的 content 和工具调用的 arguments。
    """

    def replace(text):
        for old, new in rules:
            text = re.sub(old, new, text) if use_regex else text.replace(old, new)
        return text

    for msg in sample["messages"]:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = replace(content)
        elif isinstance(content, list):  # [{"type": "text", "text": "..."}]
            for part in content:
                if part.get("type") == "text":
                    part["text"] = replace(part["text"])

        for call in msg.get("tool_calls", []):
            call["function"]["arguments"] = replace(call["function"]["arguments"])
    return sample


def events_to_openai(events, tool_name="exec_command"):
    """运行事件列表转为 OpenAI 风格样本，不补造 user/system 上下文。

    agent_message.text -> assistant.content；command_execution -> tool_calls
    和 tool 结果。按事件顺序保留调用与结果，忽略统计、状态和 reasoning。
    仅接受完成的工具链路；只有 completed 快照时也能恢复调用。
    """
    if not isinstance(events, list):
        raise ValueError("events 必须是事件列表")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("工具名称必须是非空字符串")

    messages, pending, finished, agent_texts = [], {}, {}, {}

    def append_call(item_id, command):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{item_id}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps({"command": command}, ensure_ascii=False),
                },
            }],
        })
        pending[item_id] = command

    for event in events:
        if not isinstance(event, dict):
            raise ValueError("每个事件必须是对象")
        event_type = event.get("type")
        if event_type not in ("item.started", "item.completed"):
            continue
        item = event.get("item", {})
        if not isinstance(item, dict):
            raise ValueError("item 必须是对象")
        item_type = item.get("type")
        if item_type not in ("agent_message", "command_execution"):
            continue
        if item_type == "agent_message" and event_type != "item.completed":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("消息和工具事件必须有非空 item.id")
        if item_type == "agent_message":
            text = item.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{item_id}: agent_message.text 必须是字符串")
            if item_id in agent_texts:
                if agent_texts[item_id] != text:
                    raise ValueError(f"{item_id}: 重复消息内容不一致")
                continue
            agent_texts[item_id] = text
            if text.strip():
                messages.append({"role": "assistant", "content": text})
            continue

        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{item_id}: command 必须是非空字符串")
        previous = pending.get(item_id, finished.get(item_id, (None,))[0])
        if previous is not None and previous != command:
            raise ValueError(f"{item_id}: 开始与完成事件的 command 不一致")
        if event_type == "item.started":
            if previous is None:
                append_call(item_id, command)
            continue
        output = item.get("aggregated_output")
        exit_code = item.get("exit_code")
        if not isinstance(output, str):
            raise ValueError(f"{item_id}: aggregated_output 必须是字符串")
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError(f"{item_id}: exit_code 必须是整数或 null")
        if item.get("status") == "in_progress":
            raise ValueError(f"{item_id}: 完成事件仍处于运行中")
        result = (command, output, exit_code)
        if item_id in finished:
            if finished[item_id] != result:
                raise ValueError(f"{item_id}: 重复工具结果不一致")
            continue
        if item_id not in pending:
            append_call(item_id, command)
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{item_id}",
            "content": json.dumps({
                "aggregated_output": output,
                "exit_code": exit_code,
            }, ensure_ascii=False),
        })
        pending.pop(item_id)
        finished[item_id] = result

    if pending:
        raise ValueError(f"工具调用缺少完成结果: {', '.join(pending)}")
    sample = {"messages": messages}
    if finished:
        sample["tools"] = [{
            "type": "function",
            "function": {
                "name": tool_name,
                "description": "执行 shell 命令，返回 aggregated_output 和 exit_code。",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }]
    return sample
