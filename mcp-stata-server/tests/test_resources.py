"""文件资源回传的单元测试：注册表、资源模板、read/register/list 工具、导出登记钩子。

真实文件用 tmp_path 生成；Stata 执行经 patch("server._run_stata_command") 短路。
"""

import base64
from unittest.mock import patch

import pytest
from fastmcp.tools.base import ToolResult

import server
from server import (
    _read_registered_file,
    _register_resource,
    _resource_lookup,
    _resource_uri,
    _stata_file_resource,
    stata_list_resources,
    stata_read_file,
    stata_register_file,
    stata_run,
    stata_save_dataset,
)


def _text(result):
    if isinstance(result, ToolResult):
        return result.content[0].text
    return result


# ---------------------------------------------------------------------------
# 注册表与 URI
# ---------------------------------------------------------------------------


def test_register_resource_registers_real_file(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\x0d\x0a")
    assert _register_resource(str(p), "test") is None
    entry = _resource_lookup(str(p))
    assert entry["path"] == server._normalize_path(str(p))
    assert entry["source"] == "test"
    assert entry["size"] == 6
    assert entry["mime"] == "image/png"  # 由扩展名推断
    assert entry["uri"].startswith("stata-file:///")


def test_register_resource_rejects_missing_file(tmp_path):
    err = _register_resource(str(tmp_path / "nope.png"), "test")
    assert err is not None and "错误" in err


def test_resource_uri_encodes_spaces_and_keeps_slashes(tmp_path):
    p = tmp_path / "my folder" / "a b.csv"
    uri = _resource_uri(str(p))
    assert uri.startswith("stata-file:///")
    assert "/my%20folder/a%20b.csv" in uri  # 空格百分号编码、斜杠保留


def test_resource_lookup_normalizes(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n")
    _register_resource(str(p), "test")
    # 直接路径可查到；模板路径已被 fastmcp 解码一次，这里不再二次 unquote
    assert _resource_lookup(str(p)) is not None
    # 未登记路径返回 None
    assert _resource_lookup(str(tmp_path / "y.csv")) is None


def test_resource_lookup_unknown_returns_none(tmp_path):
    assert _resource_lookup(str(tmp_path / "ghost.csv")) is None


def test_resource_lookup_does_not_double_unquote(tmp_path):
    """文件名含字面 %xx 时不得二次解码（否则查表 miss 成功能性拒绝）。"""
    p = tmp_path / "a%20b.csv"
    p.write_text("x\n")
    _register_resource(str(p), "test")
    assert _resource_lookup(str(p)) is not None


# ---------------------------------------------------------------------------
# 资源模板处理器（安全边界）
# ---------------------------------------------------------------------------


def test_stata_file_resource_returns_bytes_for_registered(tmp_path):
    p = tmp_path / "chart.png"
    payload = b"\x89PNG fake image bytes"
    p.write_bytes(payload)
    _register_resource(str(p), "stata_graph")
    assert _stata_file_resource(str(p)) == payload


def test_stata_file_resource_rejects_unregistered(tmp_path):
    p = tmp_path / "secret.txt"
    p.write_text("do not read")
    with pytest.raises(ValueError):
        _stata_file_resource(str(p))


def test_resource_template_reads_via_fastmcp(tmp_path):
    """走真实 fastmcp resources/read 链路：模板 {path*} 匹配并返回二进制。"""
    import asyncio

    p = tmp_path / "chart.png"
    payload = b"\x89PNG fake"
    p.write_bytes(payload)
    _register_resource(str(p), "stata_graph")
    rr = asyncio.run(server.mcp.read_resource(_resource_uri(str(p))))
    content = rr.contents[0].content  # 处理器返回 bytes 时直接就是原始字节
    assert content == payload


def test_resource_template_read_unknown_uri_raises(tmp_path):
    import asyncio

    p = tmp_path / "not_registered.csv"
    p.write_text("x\n")
    with pytest.raises(Exception):
        asyncio.run(server.mcp.read_resource(_resource_uri(str(p))))


def test_read_registered_file_respects_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_MAX_RESOURCE_READ_BYTES", 10)
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 100)
    _register_resource(str(p), "test")
    data, entry, err = _read_registered_file(str(p))
    assert data is None
    assert entry is not None
    assert err is not None and "过大" in err


def test_read_registered_file_returns_bytes(tmp_path):
    p = tmp_path / "ok.csv"
    p.write_text("a,b\n1,2\n")
    _register_resource(str(p), "test")
    data, _, err = _read_registered_file(str(p))
    assert err is None
    assert data == b"a,b\n1,2\n"


# ---------------------------------------------------------------------------
# stata_read_file / stata_register_file / stata_list_resources
# ---------------------------------------------------------------------------


def test_read_file_info_and_read(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("x\n1\n")
    _register_resource(str(p), "stata_export_delimited")
    info = _text(stata_read_file(str(p)))
    assert "资源 URI" in info and "data.csv" in info
    b64 = _text(stata_read_file(str(p), action="read"))
    assert b64 == base64.b64encode(b"x\n1\n").decode("ascii")


def test_read_file_rejects_unregistered(tmp_path):
    p = tmp_path / "nope.csv"
    p.write_text("x\n")
    result = stata_read_file(str(p))
    assert isinstance(result, ToolResult) and result.is_error
    assert "未登记" in _text(result)


def test_read_file_rejects_bad_action(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("x\n")
    _register_resource(str(p), "test")
    result = stata_read_file(str(p), action="bogus")
    assert isinstance(result, ToolResult) and result.is_error


def test_register_file_explicit(tmp_path):
    p = tmp_path / "manual.csv"
    p.write_text("a\n1\n")
    result = stata_register_file(str(p))
    assert "资源 URI" in _text(result)
    assert _resource_lookup(str(p)) is not None


def test_register_file_missing(tmp_path):
    result = stata_register_file(str(tmp_path / "missing.csv"))
    assert isinstance(result, ToolResult) and result.is_error


def test_list_resources_empty_and_populated(tmp_path):
    assert "没有" in _text(stata_list_resources())
    p = tmp_path / "a.csv"
    p.write_text("x\n")
    _register_resource(str(p), "stata_export_delimited")
    listing = _text(stata_list_resources())
    assert "a.csv" in listing and "stata_export_delimited" in listing


# ---------------------------------------------------------------------------
# 导出工具的登记钩子
# ---------------------------------------------------------------------------


def test_save_dataset_registers_on_success(tmp_path):
    p = tmp_path / "out.dta"
    p.write_bytes(b"\x93\x11stata mock")
    with patch("server._run_stata_command", return_value="saved"):
        result = stata_save_dataset(str(p), replace=True)
    assert isinstance(result, str)
    assert "已登记为资源" in result
    assert _resource_lookup(str(p)) is not None


def test_save_dataset_does_not_register_on_error(tmp_path):
    p = tmp_path / "out.dta"
    p.write_bytes(b"\x93\x11")
    with patch("server._run_stata_command", return_value=server._make_error_result("错误: 保存失败")):
        result = stata_save_dataset(str(p))
    assert isinstance(result, ToolResult) and result.is_error
    assert _resource_lookup(str(p)) is None


def test_stata_run_save_output_registers_and_passes_path(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("dummy")
    with patch("server._run_stata_command", return_value="ok") as mock_run:
        result = stata_run("display 1", save_output=str(p))
    assert "资源 URI" in result
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("full_output_path") == server._normalize_path(str(p))
    assert _resource_lookup(str(p)) is not None


def test_stata_run_save_output_error_still_registers(tmp_path):
    """命令链执行出错但文件已被写入（含错误文本的完整输出）：应登记。"""
    p = tmp_path / "err.txt"
    p.write_text("dummy")

    def fake_run(cmd, page=1, timeout=60, require_file=None, full_output_path=None):
        # 模拟真实 _run_stata_command：截断 + 写入错误输出
        with open(full_output_path, "wb") as f:
            f.write("[返回码: 1] boom\n".encode())
        return server._make_error_result("[返回码: 1] boom")

    with patch("server._run_stata_command", side_effect=fake_run):
        result = stata_run("display 1", save_output=str(p))
    assert isinstance(result, ToolResult) and result.is_error
    assert "完整输出已保存" in _text(result)
    assert _resource_lookup(str(p)) is not None


def test_stata_run_save_output_early_rejection_not_registered(tmp_path):
    """空命令/超长命令在 _run_stata_command 内早退（不触碰文件）：不得登记陈旧文件。"""
    p = tmp_path / "stale.txt"
    p.write_text("old stale content")
    with patch("server._run_stata_command", return_value=server._make_error_result("(无有效命令)")):
        result = stata_run("   ", save_output=str(p))
    assert isinstance(result, ToolResult) and result.is_error
    assert "完整输出已保存" not in _text(result)
    assert _resource_lookup(str(p)) is None
    assert p.read_text() == "old stale content"  # 陈旧文件原封未动


def test_stata_run_save_output_path_validated(tmp_path):
    bad = f"{tmp_path}\x00nul"
    result = stata_run("display 1", save_output=bad)
    assert isinstance(result, ToolResult) and result.is_error


def test_register_resource_preserves_original_source(tmp_path):
    """重复登记保留原始来源（实战发现：覆盖会丢元数据）。"""
    p = tmp_path / "x.dta"
    p.write_bytes(b"\x93\x11")
    _register_resource(str(p), "stata_save_dataset")
    _register_resource(str(p), "stata_register_file")  # 再次登记
    assert _resource_lookup(str(p))["source"] == "stata_save_dataset"
