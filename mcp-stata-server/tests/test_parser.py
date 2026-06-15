from server import _parse_command_blocks


def test_simple_multiline_split():
    blocks = _parse_command_blocks("summarize mpg\ntabulate foreign")
    assert blocks == ["summarize mpg", "tabulate foreign"]


def test_blank_lines_and_comments_filtered():
    cmd = """
* this is a full-line comment
// another full-line comment

summarize mpg

// inline comment
// yet another comment
tabulate foreign
"""
    blocks = _parse_command_blocks(cmd)
    assert blocks == ["summarize mpg", "tabulate foreign"]


def test_continuation_merge():
    blocks = _parse_command_blocks("regress mpg ///\n    weight")
    assert len(blocks) == 1
    assert "regress mpg" in blocks[0]
    assert "weight" in blocks[0]


def test_continuation_with_trailing_comment_text():
    # /// 后面跟注释文本也应视为续行符，忽略注释文本
    blocks = _parse_command_blocks("display /// this is a continuation comment\n    42")
    assert len(blocks) == 1
    assert "display" in blocks[0]
    assert "42" in blocks[0]
    assert "this is a continuation comment" not in blocks[0]


def test_continuation_merges_token_across_lines():
    # /// 紧跟 token 后时不应插入空格，允许 token 跨行拼接
    blocks = _parse_command_blocks("gen fo///\no = 1")
    assert len(blocks) == 1
    assert "gen foo = 1" in blocks[0]
    assert "gen fo o" not in blocks[0]


def test_continuation_with_inline_comment_on_next_line():
    # /// line merges with next line even when next line has a // comment.
    blocks = _parse_command_blocks("regress mpg ///\n    weight // control variable")
    assert len(blocks) == 1
    assert "regress mpg" in blocks[0]
    assert "weight" in blocks[0]
    assert "//" not in blocks[0]


def test_continuation_at_line_start_is_comment():
    # A line that starts with /// (not as a continuation of previous line) should be treated as a comment.
    blocks = _parse_command_blocks("summarize mpg\n/// this is a comment\ntabulate foreign")
    assert blocks == ["summarize mpg", "tabulate foreign"]


def test_compound_block_spans_lines():
    cmd = """capture noisily {
    summarize mpg
    tabulate foreign
}"""
    blocks = _parse_command_blocks(cmd)
    assert len(blocks) == 1
    assert "capture noisily {" in blocks[0]
    assert "summarize mpg" in blocks[0]
    assert "tabulate foreign" in blocks[0]
    assert blocks[0].strip().endswith("}")


def test_block_comment_spans_lines():
    cmd = """regress /*
    this is a
    multiline comment
*/ mpg weight"""
    blocks = _parse_command_blocks(cmd)
    assert len(blocks) == 1
    assert "regress" in blocks[0]
    assert "mpg" in blocks[0]
    assert "weight" in blocks[0]
    assert "this is a" not in blocks[0]


def test_block_comment_inside_string_is_preserved():
    cmd = '''display "value /* not a comment */"'''
    blocks = _parse_command_blocks(cmd)
    assert len(blocks) == 1
    assert "/* not a comment */" in blocks[0]


def test_compound_string_with_embedded_quotes():
    # Stata 复合字符串 '" ... "' 允许内部包含普通双引号
    cmd = '''display `"hello "world" end"' '''
    blocks = _parse_command_blocks(cmd)
    assert len(blocks) == 1
    assert 'hello "world" end' in blocks[0]
    # 复合字符串外的 // 才被视为注释
    assert "//" not in blocks[0] or 'hello "world" end' in blocks[0]


def test_mixed_comments_continuation_and_compound():
    cmd = """* setup
sysuse auto, ///
    clear

// run some stats
capture noisily {
    summarize mpg
    regress mpg weight
}
"""
    blocks = _parse_command_blocks(cmd)
    assert len(blocks) == 2
    assert "sysuse auto," in blocks[0]
    assert "clear" in blocks[0]
    assert "capture noisily {" in blocks[1]
    assert "regress mpg weight" in blocks[1]


def test_continuation_empty_line_ends_continuation():
    # /// 续行后的空行应中断续行，不要把 gen b=2 拼接到 gen a=1
    blocks = _parse_command_blocks("gen a = 1 ///\n\ngen b = 2")
    assert len(blocks) == 2
    assert blocks[0] == "gen a = 1"
    assert blocks[1] == "gen b = 2"


def test_continuation_comment_line_ends_continuation():
    # /// 续行链中间的 /// comment 行应中断续行
    blocks = _parse_command_blocks("gen a = 1 ///\n/// this is a comment\ngen b = 2")
    assert len(blocks) == 2
    assert blocks[0] == "gen a = 1"
    assert blocks[1] == "gen b = 2"


def test_continuation_continues_with_indented_line():
    # 正常缩进续行目标行仍应合并
    blocks = _parse_command_blocks("regress mpg ///\n    weight")
    assert len(blocks) == 1
    assert "regress mpg" in blocks[0]
    assert "weight" in blocks[0]


def test_empty_input_returns_empty_list():
    assert _parse_command_blocks("") == []
    assert _parse_command_blocks("   \n\n  ") == []


def test_unclosed_block_comment_drops_trailing_text():
    cmd = """summarize mpg
/* this comment never ends"""
    blocks = _parse_command_blocks(cmd)
    assert len(blocks) == 1
    assert "summarize mpg" in blocks[0]
    assert "never ends" not in blocks[0]


def test_unmatched_brace_emits_remaining_buffer():
    # Parser does not enforce brace balance; unmatched '{' stays in buffer
    # and gets emitted so Stata can report the syntax error.
    blocks = _parse_command_blocks("capture noisily {\nsummarize mpg")
    assert len(blocks) == 1
    assert "capture noisily {" in blocks[0]


def test_leading_space_star_is_not_comment():
    # Stata *-comments must start at column 1; leading spaces keep the line as code.
    blocks = _parse_command_blocks("  *not a comment\nsummarize mpg")
    assert len(blocks) == 2
    assert "*not a comment" in blocks[0]
    assert "summarize mpg" in blocks[1]


def test_standalone_triple_slash_ends_prior_block():
    blocks = _parse_command_blocks("summarize mpg\n///\ntabulate foreign")
    assert len(blocks) == 2
    assert "summarize mpg" in blocks[0]
    assert "tabulate foreign" in blocks[1]


def test_nested_compound_blocks_merge():
    blocks = _parse_command_blocks("capture noisily {\nforeach v of varlist mpg price {\n    summarize `v'\n}\n}")
    assert len(blocks) == 1
    assert "capture noisily {" in blocks[0]
    assert "foreach" in blocks[0]


def test_parser_ignores_carriage_returns():
    blocks = _parse_command_blocks("summarize mpg\r\ntabulate foreign")
    assert blocks == ["summarize mpg", "tabulate foreign"]
