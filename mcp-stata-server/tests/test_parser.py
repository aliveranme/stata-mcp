import pytest

from server import UnbalancedBlockError, _parse_command_blocks


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
    cmd = """display `"hello "world" end"' """
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


@pytest.mark.parametrize(
    ("cmd", "missing"),
    [
        ("capture noisily {", "}"),
        ("capture noisily {\nsummarize mpg", "}"),
        ("forvalues i=1/2 {\n    if 1 {\n        display 1\n    }", "}"),
        ("program define hi\n    display 1", "end"),
        ("input x y\n1 2", "end"),
        ("mata:\n  1+1", "end"),
    ],
)
def test_unclosed_block_raises_instead_of_hanging_session(cmd, missing):
    """未闭合的块不能送去执行 —— Stata 会等待后续输入并挂死整个会话。

    旧行为是把残缺块原样发出，注释里写着「让 Stata 报语法错」；实测
    `capture noisily {` 单独一行会让会话无响应，看门狗的 SetBreak 也救不回。
    """
    with pytest.raises(UnbalancedBlockError) as exc:
        _parse_command_blocks(cmd)
    assert missing in str(exc.value)


def test_unbalanced_error_carries_parsed_content():
    """异常须带出已解析内容，否则安全护栏会对未闭合的危险命令失效。"""
    with pytest.raises(UnbalancedBlockError) as exc:
        _parse_command_blocks("summarize price\nmata:\n  1+1")
    assert exc.value.blocks == ["summarize price"]
    assert "mata:" in exc.value.pending


def test_balanced_blocks_still_parse():
    """闭合的块不受影响。"""
    assert len(_parse_command_blocks("capture noisily {\n    display 1\n}")) == 1
    assert len(_parse_command_blocks("program define hi\n    display 1\nend")) == 1


def test_leading_space_star_is_a_comment():
    """缩进的 ``*`` 仍是注释 —— 本测试此前编码了相反的假设。

    旧断言写着「Stata *-comments must start at column 1」，真机（Stata 19.5 MP，
    批处理）反证：顶层的 ``   * comment`` 与循环体内的缩进注释都被正常当注释处理，
    无输出无错误。循环体内缩进写注释是绝大多数 Stata 代码的写法，按第 1 列判定会
    让注释里的 ``{`` / ``}`` 计入花括号深度并拒掉合法脚本。
    """
    blocks = _parse_command_blocks("  *a comment\nsummarize mpg")
    assert blocks == ["summarize mpg"]


def test_standalone_triple_slash_ends_prior_block():
    blocks = _parse_command_blocks("summarize mpg\n///\ntabulate foreign")
    assert len(blocks) == 2
    assert "summarize mpg" in blocks[0]
    assert "tabulate foreign" in blocks[1]


def test_nested_compound_blocks_merge():
    blocks = _parse_command_blocks(
        "capture noisily {\nforeach v of varlist mpg price {\n    summarize `v'\n}\n}"
    )
    assert len(blocks) == 1
    assert "capture noisily {" in blocks[0]
    assert "foreach" in blocks[0]


def test_parser_ignores_carriage_returns():
    blocks = _parse_command_blocks("summarize mpg\r\ntabulate foreign")
    assert blocks == ["summarize mpg", "tabulate foreign"]


# --- end 配对块（program / input / mata）------------------------------------
# 这类块若被拆成单行分别执行，首行会让 Stata 进入等待输入状态并挂死会话
# （实测 Stata 19.5 MP，看门狗 SetBreak 也无法恢复），故必须整块收集。


def test_program_define_collected_as_single_block():
    blocks = _parse_command_blocks(
        'program define hi\n    display "hi"\nend'
    )
    assert blocks == ['program define hi\n    display "hi"\nend']


def test_program_without_define_keyword_also_collected():
    """program name 省略 define 时同样进入定义模式。"""
    blocks = _parse_command_blocks('program hi\n    display "hi"\nend')
    assert len(blocks) == 1
    assert blocks[0].endswith("end")


def test_program_drop_is_not_a_definition_block():
    """drop/dir/list 子命令不进入定义模式，不应吞掉后续命令。"""
    blocks = _parse_command_blocks("program drop _all\nsummarize price")
    assert blocks == ["program drop _all", "summarize price"]


def test_input_block_collected_with_data_rows():
    blocks = _parse_command_blocks("clear\ninput x y\n1 2\n3 4\nend\nlist")
    assert blocks == ["clear", "input x y\n1 2\n3 4\nend", "list"]


def test_mata_block_collected():
    blocks = _parse_command_blocks("mata:\n  1+1\nend")
    assert blocks == ["mata:\n  1+1\nend"]


def test_command_after_end_block_is_separate():
    blocks = _parse_command_blocks(
        'program define hi\n    display "hi"\nend\nhi\nsummarize price'
    )
    assert len(blocks) == 3
    assert blocks[1] == "hi"
    assert blocks[2] == "summarize price"


# --- end 块的开启行带 /// ------------------------------------------------------
# 与「块内出现 ///」互为镜像。has_cont 分支直接 continue，绕过了唯一设置
# in_end_block 的那行，于是 `program`/`input` 一词落在被合并的上一行时整个块
# 判定失效，首行被单独送执行 → Stata 进入定义模式挂死会话。


def test_program_opening_line_with_continuation_stays_one_block():
    blocks = _parse_command_blocks(
        "program define mymean ///\n    , rclass\n    summarize price\nend"
    )
    assert len(blocks) == 1
    assert "program define mymean , rclass" in blocks[0]
    assert blocks[0].rstrip().endswith("end")


# --- end 块的开启行带通用前缀 --------------------------------------------------
# `quietly program define …` / `capture input …` 都是合法且可用的 Stata（真机验证
# quietly program define 定义成功、随后调用正常打印）。而 _opens_end_block 只看
# head[0]，前缀让它一律返回 False，开启行被单独送执行 → 进入定义模式挂死会话。


@pytest.mark.parametrize(
    "opener",
    [
        "quietly program define foo",
        "qui program define foo",
        "capture program define foo",
        "capture noisily program define foo",
        "by foreign: program define foo",
    ],
)
def test_prefixed_program_block_stays_one_block(opener):
    blocks = _parse_command_blocks(f'{opener}\n    display "x"\nend')
    assert len(blocks) == 1
    assert blocks[0].rstrip().endswith("end")


def test_prefixed_input_block_stays_one_block():
    blocks = _parse_command_blocks("quietly input x\n1\n2\nend\nlist")
    assert blocks == ["quietly input x\n1\n2\nend", "list"]


def test_prefixed_program_drop_still_not_a_block():
    """前缀剥离后仍须正确识别 drop/dir/list 不进入定义模式。"""
    blocks = _parse_command_blocks("capture program drop _all\nsummarize price")
    assert blocks == ["capture program drop _all", "summarize price"]


# --- 注释与续行：`///` 先拼逻辑行，再判是不是注释 --------------------------------
# 真机 ground truth（Stata 19.5 MP，批处理日志）：
#   ·    * 缩进注释            → 合法，无输出无错误（顶层与循环体内都成立）
#   · `display 1 ///` + `* 2`  → 输出 **2**：续行后的 `*` 是乘号，不是注释
#   · `* 注释 ///` + 后续行     → 后续行被并入注释，一行都不执行
# 因此判定顺序必须是「先按 /// 拼成逻辑行，再看逻辑行是否以 * 开头」。


def test_indented_star_comment_is_a_comment():
    blocks = _parse_command_blocks('   * indented comment\ndisplay "x"')
    assert blocks == ['display "x"']


def test_indented_star_comment_braces_not_counted():
    """缩进注释里的 { } 不得计入花括号深度，否则合法循环被拒或被切碎。"""
    blocks = _parse_command_blocks(
        "forvalues i=1/2 {\n    * body { brace in comment\n    display `i'\n}"
    )
    assert len(blocks) == 1
    assert blocks[0].rstrip().endswith("}")

    blocks = _parse_command_blocks(
        "forvalues i=1/2 {\n    * close } here\n    display `i'\n}"
    )
    assert len(blocks) == 1


def test_star_comment_with_continuation_swallows_next_line():
    """`* 注释 ///` 在 Stata 中吞掉下一行；解析器不得反而去执行它。"""
    assert _parse_command_blocks('* DO NOT RUN ///\ndisplay "THIS RAN"') == []
    assert _parse_command_blocks('   * indented ///\ndisplay "THIS RAN"') == []


def test_star_comment_continuation_chain_swallows_all():
    blocks = _parse_command_blocks(
        '* c ///\ndisplay "A" ///\ndisplay "B"\ndisplay "after"'
    )
    assert blocks == ['display "after"']


def test_star_after_continuation_is_multiplication_not_comment():
    """`display 1 ///` 之后的 `* 2` 是乘号（真机输出 2），不能当注释丢掉。"""
    assert _parse_command_blocks("display 1 ///\n* 2") == ["display 1 * 2"]
    assert _parse_command_blocks("display 1 ///\n   * 2") == ["display 1 * 2"]


def test_block_comment_across_lines_is_a_line_join():
    """`/*` 换行 `*/` 是官方行连接符（`///` 出现前的写法），须并入上一行。

    真机对照：``regress price weight /*\\n*/ mpg foreign`` 的
    ``e(cmdline)`` 为 ``regress price weight  mpg foreign``、``e(df_m)=3``。
    劈成两行会变成只有一个回归元的**另一个模型**，且前半条独立执行「成功」。
    """
    assert _parse_command_blocks("regress price weight /*\n*/ mpg foreign") == [
        "regress price weight  mpg foreign"
    ]


def test_block_comment_spanning_multiple_lines_joins_once():
    blocks = _parse_command_blocks("regress price weight /*\n  still comment\n*/ mpg")
    assert blocks == ["regress price weight  mpg"]


def test_standalone_block_comment_does_not_join_separate_commands():
    blocks = _parse_command_blocks('display 1\n/* note\n*/ display 2')
    assert len(blocks) == 2
    assert blocks[0] == "display 1"
    assert blocks[1].strip() == "display 2"


def test_input_opening_line_with_continuation_stays_one_block():
    blocks = _parse_command_blocks('input str30 a ///\n    int c\n"x" 1\nend')
    assert len(blocks) == 1
    assert "input str30 a int c" in blocks[0]


def test_command_after_continuation_opened_block_is_separate():
    blocks = _parse_command_blocks(
        "program define hi ///\n    , rclass\n    display 1\nend\nhi"
    )
    assert len(blocks) == 2
    assert blocks[1] == "hi"


# --- Stata 复合双引号 `" ... "' -----------------------------------------------
# 开启是反引号+双引号，结束是双引号+单引号。曾把开启符写成 "'（那其实是结束符），
# 导致普通字符串里一出现 "' 就翻转状态 —— `title("'90s")` 这类以撇号开头的
# 字符串会让行尾的 /// 被当成字符串内容，续行失效。


def test_apostrophe_leading_string_does_not_break_continuation():
    blocks = _parse_command_blocks(
        'twoway scatter y x, title("\'90s") ///\n  name(g1)'
    )
    assert len(blocks) == 1, "撇号开头的字符串不应让 /// 失效"
    assert "name(g1)" in blocks[0]


def test_compound_quote_opener_is_backtick_quote():
    blocks = _parse_command_blocks('display `"hello "world" end"\'')
    assert len(blocks) == 1
    assert 'hello "world" end' in blocks[0]


def test_compound_quote_protects_comment_and_brace():
    """复合引号内的 // 与 { 不应被当作注释或块开始。"""
    assert _parse_command_blocks('display `"http://x.com"\'') == ['display `"http://x.com"\'']
    assert len(_parse_command_blocks('display `"a{b"\'')) == 1


def test_apostrophe_string_with_trailing_comment():
    blocks = _parse_command_blocks('gen d = "\'90s" if year<2000 // 注释')
    assert len(blocks) == 1
    assert "'90s" in blocks[0]
    assert "注释" not in blocks[0]
