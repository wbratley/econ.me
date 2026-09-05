"""replay_prompt: the persisted-prompt parser and model builder.

The round-trip that matters: what AgentLoop._persist_prompt wrote is
exactly what parse_prompt_file reads back (header, SYSTEM, USER
attempts), so a burn can be re-fired offline against the provider.
"""

import pytest

from experiments.agent.replay_prompt import build_model, parse_prompt_file


def test_parse_prompt_file_round_trip(tmp_path):
    f = tmp_path / "seat-house-harald.round-3.author.prompt.md"
    f.write_text(
        "# model: nim:openai/gpt-oss-20b\n"
        "# seat: House Harald\n"
        "# round: 3\n"
        "# kind: author\n"
        "\n"
        "## SYSTEM\n"
        "the rules\n"
        "\n"
        "## USER (attempt 1)\n"
        "first ask\n"
        "\n"
        "## USER (attempt 2)\n"
        "second ask with lint feedback\n")
    parsed = parse_prompt_file(f)
    assert parsed["model"] == "nim:openai/gpt-oss-20b"
    assert parsed["seat"] == "House Harald"
    assert parsed["round"] == "3"
    assert parsed["kind"] == "author"
    assert parsed["system"] == "the rules"
    assert parsed["users"] == ["first ask",
                               "second ask with lint feedback"]


def test_parse_rejects_non_prompt_files(tmp_path):
    f = tmp_path / "README.md"
    f.write_text("# a plain markdown file\n\nno sections here\n")
    parsed = parse_prompt_file(f)
    assert parsed["model"] == "" and parsed["users"] == []


def test_build_model_routes_by_prefix():
    from experiments.agent.llm import DeepSeekModel, NimModel

    m = build_model("deepseek:deepseek-v4-flash", None)
    assert isinstance(m, DeepSeekModel)
    assert m.name == "deepseek:deepseek-v4-flash"
    n = build_model("nim:openai/gpt-oss-20b", None)
    assert isinstance(n, NimModel)
    assert n.name == "nim:openai/gpt-oss-20b"
    bare = build_model("openai/gpt-oss-20b", None)
    assert isinstance(bare, NimModel) and bare._model == "openai/gpt-oss-20b"
