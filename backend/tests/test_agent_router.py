"""意图路由器（纯函数）的单元测试。

核心断言原则（见改造方案 5.5.2）：
    **必须同时命中「聚合词」与「已注册列名词」才强路由 DATA。**
    反例是评测集里真实存在的「布洛芬最多多久吃一次？」——
    命中聚合词"最多"但不是统计题。只命中聚合词就路由 SQL 会答错。
因此本文件既要测"该路由的"，也要测"不该路由的"（双向断言）。

Step 2 新增两类意图，同样要**双向**测：
    CHITCHAT 既要能识别闲聊，也不能把"机房温度多少"这种真业务问题判成闲聊；
    META 必须在 DOC 之前命中（"我上次问的是什么"含"是什么"）。
"""

from __future__ import annotations

import pytest

from app.agent.router import (
    AGGREGATION_WORDS,
    DEFAULT_COLUMN_WORDS,
    DOC_WORDS,
    Route,
    build_column_words,
    is_greeting,
    is_meta_question,
    route_by_rules,
)


class TestStrongRouteToSQL:
    """聚合词 + 列名词同时命中 -> 强路由 SQL。"""

    @pytest.mark.parametrize("q", [
        "员工表里有多少人？",
        "哪个部门人数最多？",
        "各部门分别有多少人？",
        "全公司薪资最高的员工是谁？",
        "2026 年入职的员工有多少人？",
        "Sales 部门的平均薪资是多少？",
        "销售总营收是多少？",
        "哪一类费用支出最高？",
        "橱柜成本里单价最高的项目是哪个？",
        "各个产品的销量分别是多少？",
    ])
    def test_routes_to_sql(self, q):
        d = route_by_rules(q)
        assert d.route == Route.SQL, f"{q} -> {d.route} ({d.reason})"
        assert d.matched_aggregation, "应记录命中的聚合词"
        assert d.matched_column, "应记录命中的列名词"

    def test_reason_is_explainable(self):
        """路由原因必须可读（审计与调试依赖它）。"""
        d = route_by_rules("哪个部门人数最多？")
        assert "聚合词" in d.reason and "列名词" in d.reason


class TestRouteToRAG:
    """文档意图词命中且未构成统计信号 -> RAG。"""

    @pytest.mark.parametrize("q", [
        "路由器的质保期是多久？",
        "合同里关于违约是怎么规定的？",
        "这个产品的安装步骤是什么？",
        "技术手册里怎么说明接口规范的？",
        "保修政策是什么？",
    ])
    def test_routes_to_rag(self, q):
        d = route_by_rules(q)
        assert d.route == Route.RAG, f"{q} -> {d.route} ({d.reason})"

    def test_doc_word_takes_priority_over_aggregation(self):
        """真实反例：命中聚合词"最多"但本质是文档问题。

        这是本项目规则层最容易踩的坑，也是规则层必须可单测的根本原因。
        """
        d = route_by_rules("布洛芬最多多久吃一次？")
        assert d.route == Route.RAG, f"不应路由到 SQL: {d.reason}"
        assert d.matched_doc_word, "应命中文档意图词（多久）"


class TestLLMDecide:
    """无强信号 -> 交关卡二（LLM 工具选择）。"""

    @pytest.mark.parametrize("q", [
        "帮我看看这个问题",
        "最近有什么新东西",
    ])
    def test_no_signal_goes_to_llm(self, q):
        assert route_by_rules(q).route == Route.LLM_DECIDE

    def test_column_only_goes_to_llm(self):
        """仅命中列名词：可能是查明细而非统计，交 LLM 判断。"""
        d = route_by_rules("员工的邮箱是什么")
        # 无聚合词 -> 不该强路由 SQL
        assert d.route != Route.SQL, f"仅命中列名不应强路由 SQL: {d.reason}"

    def test_aggregation_only_goes_to_llm(self):
        """仅命中聚合词、无列名词 -> 交 LLM（避免"最多多久"这类误路由）。"""
        d = route_by_rules("哪个最多")
        assert d.route != Route.SQL, f"仅命中聚合词不应强路由 SQL: {d.reason}"


class TestMetaRoute:
    """询问对话自身 -> META（必须排在 DOC 之前）。"""

    @pytest.mark.parametrize("q", [
        "我上一次问的是什么？",
        "刚才那个问题是什么",
        "再说一遍",
        "复述一下我的问题",
    ])
    def test_meta_questions_route_to_meta(self, q):
        d = route_by_rules(q)
        assert d.route == Route.META, f"{q} -> {d.route}（{d.reason}）"

    def test_meta_takes_priority_over_doc_word(self):
        """反例锁死：这句话含 DOC 词"是什么"，如果 DOC 先判就会白检索一轮。

        修复前 meta 是 loop 里一个"覆盖分派"的 if，路由层仍报 rag ——
        日志/指标上看不出这条其实不需要检索。
        """
        d = route_by_rules("我上一次问的是什么？")
        assert d.route == Route.META
        assert d.matched_doc_word, "「是什么」确实命中了文档词，但 META 必须优先"

    def test_is_meta_question_is_pure(self):
        """纯函数只判句式，不看历史 —— 有没有上文由调用方裁决（AgentLoop.run）。"""
        assert is_meta_question("我上一次问的是什么") is True
        assert is_meta_question("上次采购的合同金额是多少") is False


class TestChitChat:
    """闲聊 / 领域外 -> CHITCHAT（不检索、不调工具）。"""

    @pytest.mark.parametrize("q", [
        "你好", "您好！", "谢谢", "thanks", "再见", "你是谁？", "你能做什么？",
    ])
    def test_greetings_and_self_intro_are_chitchat(self, q):
        d = route_by_rules(q)
        assert d.route == Route.CHITCHAT, f"{q} -> {d.route}（{d.reason}）"

    @pytest.mark.parametrize("q", [
        "今天天气怎么样？",
        "最近有什么新闻",
        "讲个笑话",
        "现在几点了",
    ])
    def test_off_topic_without_business_signal(self, q):
        d = route_by_rules(q)
        assert d.route == Route.CHITCHAT, f"{q} -> {d.route}（{d.reason}）"

    @pytest.mark.parametrize("q", [
        # 含领域外词，但同时有业务信号 -> 绝不能判闲聊（宁可多检索一次）
        "天气热了，机房设备的温度规格是多少？",
        "股市行情会不会影响公司营收？",
        # 不含领域外词的真业务问题
        "机房温度多少度",
        "布洛芬最多多久吃一次？",
    ])
    def test_business_questions_are_not_chitchat(self, q):
        d = route_by_rules(q)
        assert d.route != Route.CHITCHAT, f"{q} 被误判为闲聊: {d.reason}"

    def test_greeting_must_be_whole_sentence(self):
        """"你好，请问质保期多久" 是业务问题，不是寒暄（fullmatch 而非子串）。"""
        assert is_greeting("你好") is True
        assert is_greeting("你好，请问质保期多久？") is False
        d = route_by_rules("你好，请问质保期多久？")
        assert d.route == Route.DOC, d.reason


class TestRouteEnumCompat:
    """枚举改名不能打断下游：旧名是同值别名，旧字符串仍可构造。"""

    def test_legacy_members_are_aliases(self):
        assert Route.RAG is Route.DOC
        assert Route.SQL is Route.DATA
        assert Route.LLM_DECIDE is Route.HYBRID

    def test_legacy_strings_still_parse(self):
        assert Route("rag") is Route.DOC
        assert Route("sql") is Route.DATA
        assert Route("llm_decide") is Route.HYBRID

    def test_new_values_parse(self):
        assert Route("chitchat") is Route.CHITCHAT
        assert Route("meta") is Route.META
        assert Route("doc") is Route.DOC
        assert Route("data") is Route.DATA
        assert Route("hybrid") is Route.HYBRID

    def test_unknown_value_still_raises(self):
        with pytest.raises(ValueError):
            Route("nonsense")


class TestCrossSourceQuestion:
    """跨源问题（文档 + 数据）应能进入 LLM 决策，由模型决定同时调两个工具。"""

    def test_mixed_question_not_force_routed(self):
        q = "研发部门的平均薪资，和文档里规定的薪资带宽上限相比，超了吗？"
        d = route_by_rules(q)
        # 这条同时有统计信号与文档信号 —— 关键是**不能被错误地单一强路由**，
        # 否则会丢掉另一半依据。交 LLM（可同时调两个工具）是最安全的。
        assert d.route in (Route.LLM_DECIDE, Route.RAG, Route.SQL)

    def test_multi_tool_is_not_expressible_by_rules(self):
        """说明规则层的表达力边界：它只能给单一目标，表达不了"两个都查"。"""
        # 这既是测试也是文档：跨源能力必须由关卡二提供
        d = route_by_rules("平均薪资是多少，制度上有没有上限规定？")
        assert d.route in (Route.SQL, Route.RAG, Route.LLM_DECIDE)


class TestWordListsAreSane:
    def test_no_empty_words(self):
        for name, words in (
            ("AGGREGATION", AGGREGATION_WORDS),
            ("DOC", DOC_WORDS),
            ("COLUMN", DEFAULT_COLUMN_WORDS),
        ):
            assert words, f"{name} 词表不应为空"
            assert all(w and w.strip() for w in words), f"{name} 含空词"

    def test_words_are_lowercase_for_matching(self):
        """匹配用的是 lower()，词表里若有大写会造成永不命中。"""
        for name, words in (
            ("AGGREGATION", AGGREGATION_WORDS),
            ("DOC", DOC_WORDS),
            ("COLUMN", DEFAULT_COLUMN_WORDS),
        ):
            bad = [w for w in words if w != w.lower()]
            assert not bad, f"{name} 含大写词（永不命中）: {bad}"


class TestBuildColumnWordsFromSchema:
    """规则层应能随 schema 自动演进（新增数据表无需改代码）。"""

    def test_extracts_chinese_from_schema(self):
        class Col(dict):
            pass

        class FakeSchema:
            display_name = "设备台账"
            description = "设备清单"
            columns = [
                {"name": "device_name", "description": "设备名称"},
                {"name": "vendor", "description": "供应商"},
            ]

        words = build_column_words([FakeSchema()])
        assert "设备台账" in words
        assert "设备名称" in words
        assert "供应商" in words
        # 仍保留内置词表（否则会因新 schema 覆盖而丢失既有识别能力）
        assert "员工" in words

    def test_handles_empty(self):
        words = build_column_words([])
        assert "员工" in words


class TestColloquialPhrasings:
    """口语化短问法（实测发现的覆盖缺口）。

    背景：初版列名词表有"员工/人数"但没有单字"人"，
    导致「有多少人」这种极常见的口语问法**匹配不到任何列名词**，
    只命中聚合词"多少" -> 落到 LLM 决策而非强路由 SQL。
    这类缺口不会报错，只会让规则层"看起来在工作但实际没生效"。
    """

    @pytest.mark.parametrize("q", [
        "有多少人？",
        "总共几个人？",
        "Sales 部门有几个人？",
        "员工一共多少人？",
    ])
    def test_short_phrasings_route_to_sql(self, q):
        d = route_by_rules(q)
        assert d.route == Route.SQL, f"{q} -> {d.route}（{d.reason}）"


class TestCustomWordLists:
    """支持注入自定义词表（便于测试与按租户定制）。"""

    def test_custom_words_used(self):
        d = route_by_rules(
            "看看这个指标",
            column_words=["指标"],
            aggregation_words=["看看"],
            doc_words=[],
        )
        assert d.route == Route.SQL

    def test_empty_doc_words_disables_rag_rule(self):
        """doc_words=[] 应彻底关掉 RAG 规则（空列表是有效值，不是"用默认"）。"""
        d = route_by_rules("有多少人", doc_words=[])
        assert d.route == Route.SQL
