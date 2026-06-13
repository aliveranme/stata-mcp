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
