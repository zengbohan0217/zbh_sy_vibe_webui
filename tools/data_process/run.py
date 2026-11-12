"""流程：读 jsonl -> 依次执行算子 -> 写 jsonl。

用法：python run.py input.jsonl output.jsonl
要增删步骤，直接改下面的 STEPS。
"""
import json
import sys

from ops import ensure_user_instruction, rename_tools, replace_text

# 按顺序执行：(算子, 参数)
STEPS = [
    (rename_tools, {"mapping": {"shell": "exec_command"}}),
    (ensure_user_instruction, {"default_text": "请完成任务。"}),
    (replace_text, {"rules": [("/Users/bohanzeng", "/home/user")]}),
]


def main(src, dst):
    ok, bad = 0, 0
    with open(src, encoding="utf-8") as fin, open(dst, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            try:
                sample = json.loads(line)
                for op, kwargs in STEPS:
                    sample = op(sample, **kwargs)
            except Exception as e:  # 单条出错不影响整体
                bad += 1
                print(f"跳过一条: {e!r}")
                continue
            fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
            ok += 1
    print(f"完成: 成功 {ok}, 失败 {bad}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
