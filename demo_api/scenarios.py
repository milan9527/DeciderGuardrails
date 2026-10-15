from dataclasses import asdict

from guardrails_bench.core import load_cases, load_policy

CHECK_LABELS = {
    "hate": "仇恨与歧视", "insults": "辱骂与骚扰", "sexual": "露骨性内容",
    "violence": "暴力威胁", "misconduct": "违法协助", "self_harm": "自伤风险",
    "prompt_attack": "提示词攻击", "pii": "敏感信息", "denied_topics": "投资建议",
    "word_filter": "禁用词", "grounding": "事实依据", "relevance": "回答相关性",
}

SCENARIOS = [
    {
        "id": "customer-privacy", "title": "客户信息保护", "category": "pii", "icon": "fingerprint",
        "description": "具体联系方式与完全脱敏的信息，应该有不同处理。",
        "goal": "识别具体敏感值，同时避免把脱敏占位符误判为泄露。",
        "cases": ["pii-1-zh-block", "pii-1-zh-allow", "adversarial-07", "adversarial-09"],
    },
    {
        "id": "prompt-injection", "title": "提示词攻击", "category": "prompt_attack", "icon": "shield",
        "description": "区分越权指令与安全培训中对攻击样本的引用。",
        "goal": "拦截操纵分类的指令，允许将攻击作为数据进行分析。",
        "cases": ["prompt_attack-2-zh-block", "prompt_attack-2-zh-allow",
                  "prompt_attack-3-en-block", "prompt_attack-3-en-allow"],
    },
    {
        "id": "account-security", "title": "账号与网络安全", "category": "misconduct", "icon": "lock",
        "description": "盗取凭据与正规的安全防御，意图看似接近，边界不同。",
        "goal": "拒绝盗号协助，允许安全教育及授权恢复。",
        "cases": ["misconduct-1-zh-block", "misconduct-1-zh-allow",
                  "misconduct-3-en-block", "misconduct-3-en-allow"],
    },
    {
        "id": "investment-advice", "title": "金融服务边界", "category": "denied_topics", "icon": "chart",
        "description": "允许一般金融知识，限制个性化的资产买卖建议。",
        "goal": "根据本演示策略区分金融教育和针对个人情况的投资建议。",
        "cases": ["denied_topics-2-zh-block", "denied_topics-2-zh-allow",
                  "denied_topics-2-en-block", "denied_topics-2-en-allow"],
    },
    {
        "id": "answer-grounding", "title": "客服回答核验", "category": "grounding", "icon": "document",
        "description": "把生成的回答与退货、配送政策逐句对照。",
        "goal": "发现与参考资料矛盾或未经支持的承诺。",
        "cases": ["grounding-1-zh-block", "grounding-1-zh-allow",
                  "grounding-2-en-block", "grounding-2-en-allow"],
    },
    {
        "id": "answer-relevance", "title": "回答是否切题", "category": "relevance", "icon": "message",
        "description": "回应物流或账号问题，不能用无关内容替代回答。",
        "goal": "允许相关回答和合理拒绝，识别答非所问。",
        "cases": ["relevance-2-zh-block", "relevance-2-zh-allow",
                  "relevance-3-en-block", "relevance-3-en-allow"],
    },
    {
        "id": "restricted-words", "title": "内部词汇保护", "category": "word_filter", "icon": "tag",
        "description": "检查指定词汇，以及大小写与空格造成的边界差异。",
        "goal": "按字面规则检查 PROJECT_ORCHID 和 绝密代号；模型结果可能与规则不一致。",
        "cases": ["word_filter-2-zh-block", "word_filter-2-zh-allow",
                  "adversarial-13", "adversarial-11"],
    },
    {
        "id": "crisis-support", "title": "危机求助与自伤", "category": "self_harm", "icon": "heart",
        "description": "拒绝自伤方法，同时让求助与支持信息得到回应。",
        "goal": "检查安全边界，避免因敏感主题本身而拒绝正常求助。",
        "cases": ["self_harm-1-zh-block", "self_harm-1-zh-allow",
                  "self_harm-2-en-block", "self_harm-2-en-allow"],
    },
]


def catalog():
    cases = {case.id: case for case in load_cases(None, load_policy())}
    return [
        {**scenario, "cases": [
            {**asdict(cases[key]), "label": (
                "应拦截 · " if cases[key].expected == "block" else "应放行 · "
            ) + ("中文" if cases[key].language == "zh" else "English")}
            for key in scenario["cases"]
        ]}
        for scenario in SCENARIOS
    ]
