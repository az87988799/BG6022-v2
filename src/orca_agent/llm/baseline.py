"""Deterministic, explainable baseline router used offline and as a safe fallback."""

from __future__ import annotations

import re

from orca_agent.domain.p7_conversation import (
    CalculationAction,
    CalculationIntent,
    ChemistryQAIntent,
    ContextQueryIntent,
    ContextSnapshot,
    GeneralQAIntent,
    MoleculeInputType,
    ResponseSource,
    TurnInterpretation,
)
from orca_agent.orchestration.p7_versions import PROMPT_VERSION_V3, TURN_SCHEMA_V3
from orca_agent.planning.p7_plan_compiler import proposal_from_operations

_CAS = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
_CID = re.compile(r"(?:pubchem\s*)?cid\s*[:：]?\s*(\d+)", re.I)
_SMILES = re.compile(r"(?:smiles?|结构)\s*[:：=]?\s*([^\s,，。；;]+)", re.I)
_TASK_ALIAS = re.compile(r"(?:任务|task)\s*([A-Za-z0-9一二三四五六七八九十]+)", re.I)
_CHARGE = re.compile(r"(?:电荷|charge)\s*[:：=]?\s*([+-]?\d+)", re.I)
_MULTIPLICITY = re.compile(r"(?:多重度|multiplicity|spin)\s*[:：=]?\s*(\d+)", re.I)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word.casefold() in text.casefold() for word in words)


def _question_like(text: str) -> bool:
    return (
        "?" in text
        or "？" in text
        or _has_any(text, ("什么", "为何", "为什么", "区别", "是否", "如何", "吗"))
    )


def _extract_molecule(text: str) -> tuple[MoleculeInputType | None, str | None]:
    match = _SMILES.search(text)
    if match:
        value = match.group(1).rstrip("，,。；;:：")
        return MoleculeInputType.SMILES, value
    match = _CAS.search(text)
    if match:
        return MoleculeInputType.CAS, match.group(0)
    match = _CID.search(text)
    if match:
        return MoleculeInputType.CID, match.group(1)
    lowered = text.casefold()
    names = (
        ("乙醇", "ethanol"),
        ("酒精", "ethanol"),
        ("ethanol", "ethanol"),
        ("水", "water"),
        ("water", "water"),
        ("benzene", "benzene"),
        ("苯", "benzene"),
    )
    for needle, value in names:
        if needle.casefold() in lowered:
            return MoleculeInputType.NAME, value
    return None, None


def _extract_task_alias(text: str) -> str | None:
    if _has_any(
        text,
        (
            "当前任务",
            "当前的任务",
            "刚才的任务",
            "this task",
            "that task",
            "current task",
        ),
    ):
        return "当前任务"
    match = _TASK_ALIAS.search(text)
    if match:
        return f"任务{match.group(1)}"
    if _has_any(text, ("刚才", "上次", "this task", "that task")):
        return "当前任务"
    return None


def _context_query_like(text: str) -> bool:
    if _extract_molecule(text)[0] is not None and _has_any(
        text, ("计算", "calculate", "compute", "算一下", "run")
    ):
        return False
    if _has_any(
        text, ("去掉", "删除", "移除", "改成只做", "remove", "drop", "补做", "追加")
    ) and _has_any(text, ("频率", "freq", "单点", "sp", "优化", "opt")):
        return False
    return _has_any(
        text,
        (
            "刚才",
            "上次",
            "当前任务",
            "this task",
            "that task",
            "算完了吗",
            "完成了吗",
            "采用什么方法",
            "使用了什么方法",
            "what method",
            "只显示",
            "仅显示",
            "保留",
            "小数",
            "单位",
            "改成",
            "查询",
            "show result",
            "status",
        ),
    )


def _extract_int(text: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(text)
    return None if match is None else int(match.group(1))


def _output_spec(text: str) -> dict[str, object]:
    quantities: list[dict[str, object]] = []
    frequency_removed = _has_any(
        text,
        (
            "不要频率",
            "不做频率",
            "不需要频率",
            "去掉频率",
            "删除频率",
            "移除频率",
            "without freq",
            "without frequency",
            "remove freq",
            "remove frequency",
            "drop freq",
            "drop frequency",
        ),
    )
    sp_removed = _has_any(
        text,
        (
            "去掉单点",
            "删除单点",
            "移除单点",
            "without sp",
            "remove sp",
            "drop sp",
        ),
    )
    opt_removed = _has_any(
        text,
        ("去掉优化", "删除优化", "移除优化", "without opt", "remove opt", "drop opt"),
    )
    if not sp_removed and _has_any(
        text, ("独立单点", "独立 sp", "independent sp", "single point energy", "单点能量")
    ):
        quantities.append(
            {"kind": "independent_sp_electronic_energy", "required": True, "source_selector": "sp"}
        )
    if not opt_removed and _has_any(
        text, ("优化结构", "优化后的结构", "optimized structure", "xyz")
    ):
        quantities.append({"kind": "optimized_geometry", "required": True})
    if not frequency_removed and _has_any(text, ("频率", "振动", "frequency", "vibrational")):
        quantities.append(
            {"kind": "vibrational_frequencies", "required": True, "source_selector": "freq"}
        )
    if _has_any(text, ("优化能量", "opt 能量", "opt energy")):
        quantities.append(
            {"kind": "opt_final_electronic_energy", "required": True, "source_selector": "opt"}
        )
    if _has_any(text, ("局部极小", "local minimum")):
        if not any(item.get("kind") == "opt_final_electronic_energy" for item in quantities):
            quantities.append(
                {"kind": "opt_final_electronic_energy", "required": True, "source_selector": "opt"}
            )
        quantities.append({"kind": "local_minimum_support", "required": True})
    if (
        not quantities
        and not sp_removed
        and _has_any(text, ("只做单点", "仅单点", "single point only", "only sp"))
    ):
        quantities.append(
            {"kind": "independent_sp_electronic_energy", "required": True, "source_selector": "sp"}
        )
    if (
        not quantities
        and not opt_removed
        and _has_any(text, ("只做优化", "仅优化", "optimization only", "only opt"))
    ):
        quantities.append({"kind": "optimized_geometry", "required": True})
    if (
        not quantities
        and not frequency_removed
        and _has_any(text, ("只做频率", "仅频率", "frequency only", "only freq"))
    ):
        quantities.append(
            {"kind": "vibrational_frequencies", "required": True, "source_selector": "freq"}
        )
    if not quantities and _has_any(text, ("能量", "energy")):
        quantities.append(
            {"kind": "independent_sp_electronic_energy", "required": True, "source_selector": "sp"}
        )
    output: dict[str, object] = {}
    if quantities:
        output["quantities"] = quantities
    if _has_any(text, ("表格", "table")):
        output["layout"] = "table"
    elif _has_any(text, ("json", "结构化")):
        output["layout"] = "json"
    else:
        output["layout"] = "prose"
    output["style"] = "detailed" if _has_any(text, ("详细", "detail")) else "concise"
    output["language"] = "en" if _has_any(text, (" in english", "用英文", "english")) else "zh"
    if _has_any(text, ("证据", "evidence", "来源")):
        output["include_evidence"] = True
    precision = re.search(
        r"(?:小数|decimal|precision)\s*(?:位|places)?\s*[:：=]?\s*(\d+)", text, re.I
    )
    if precision:
        output["precision"] = int(precision.group(1))
    return output


def _query_output_spec(text: str) -> dict[str, object]:
    if _has_any(text, ("算完了吗", "完成了吗", "状态", "status")) and not _has_any(
        text, ("能量", "energy", "频率", "frequency", "结构", "geometry")
    ):
        return {
            "quantities": [{"kind": "execution_status", "required": True}],
            "layout": "prose",
        }
    return (
        _output_spec(text)
        if _has_any(
            text,
            (
                "只显示",
                "仅显示",
                "独立单点",
                "单点能量",
                "优化结构",
                "频率",
                "小数",
                "单位",
                "table",
            ),
        )
        else {}
    )


def _operations(text: str) -> tuple[str, ...]:
    if _has_any(text, ("只做单点", "仅单点", "single point only", "only sp")):
        return ("sp",)
    if _has_any(text, ("只做优化", "仅优化", "optimization only", "only opt")):
        return ("opt",)
    if _has_any(text, ("只做频率", "仅频率", "frequency only", "only freq")):
        return ("freq",)
    if _has_any(text, ("完整", "全流程", "all steps", "complete workflow")):
        return ("opt", "freq", "sp")
    global_minimum = _has_any(text, ("全局最低", "global minimum", "所有构象", "构象搜索"))
    if not global_minimum and _has_any(
        text, ("最低能量", "最低", "local minimum", "局部极小", "local-minimum")
    ):
        return ("opt", "freq")
    frequency_prohibited = _has_any(
        text,
        ("不要频率", "不做频率", "不需要频率", "without freq", "without frequency", "no frequency"),
    )
    has_opt = _has_any(text, ("优化", "optimize", "optimization", "opt"))
    has_freq = not frequency_prohibited and _has_any(
        text, ("频率", "振动", "frequency", "vibrational", "freq")
    )
    has_sp = _has_any(text, ("独立单点", "单点", "single point", "sp"))
    if has_opt and has_freq:
        return ("opt", "freq")
    if has_opt and has_sp:
        return ("opt", "sp")
    if has_opt:
        return ("opt",)
    if has_freq:
        return ("freq",)
    # An output request such as “independent single-point energy” selects a
    # result, not necessarily a shortened workflow.  Legacy callers use the
    # empty tuple as the historical complete-chain signal; the v3 proposal
    # below carries the semantic SP-only candidate for new profiles.
    return ()


def _candidate_operations(text: str) -> tuple[str, ...]:
    operations = _operations(text)
    if operations:
        return operations
    # An explicit single-point phrase is an execution-scope request.  A bare
    # "energy"/"only show energy" phrase is output scope only and must not be
    # silently converted into an SP run.
    if _has_any(
        text,
        (
            "独立单点",
            "独立 sp",
            "single point",
            "single-point",
            "only sp",
            "只做单点",
            "仅单点",
        ),
    ):
        return ("sp",)
    return ()


def _proposal_outputs(
    output_spec: dict[str, object], operations: tuple[str, ...]
) -> tuple[str, ...]:
    raw = output_spec.get("quantities")
    if isinstance(raw, list):
        return tuple(
            str(item["kind"])
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
        )
    defaults = {
        ("opt",): ("optimized_geometry",),
        ("sp",): ("independent_sp_electronic_energy",),
        ("freq",): ("vibrational_frequencies", "local_minimum_support"),
        ("opt", "freq"): (
            "opt_final_electronic_energy",
            "vibrational_frequencies",
            "local_minimum_support",
        ),
        ("opt", "sp"): ("optimized_geometry", "independent_sp_electronic_energy"),
        ("opt", "freq", "sp"): ("independent_sp_electronic_energy",),
    }
    return defaults.get(operations, ("execution_status",))


def _calculation(
    text: str, *, action: CalculationAction = CalculationAction.PLAN_NEW
) -> CalculationIntent:
    molecule_kind, molecule_value = _extract_molecule(text)
    charge = _extract_int(text, _CHARGE)
    multiplicity = _extract_int(text, _MULTIPLICITY)
    method = None
    environment = None
    if re.search(r"b3lyp|m06|pbe0|other basis|其他基组|基组", text, re.I):
        method_match = re.search(r"(B3LYP|M06|PBE0|其他基组|基组[^，。 ]*)", text, re.I)
        method = method_match.group(1) if method_match is not None else "unsupported_method_request"
    if _has_any(text, ("水溶液", "溶液", "solvent", "aqueous", "solution")):
        environment = "solvent"
    prohibited: list[str] = []
    if _has_any(text, ("gibbs", "自由能", "吉布斯", "zpe", "零点能")):
        prohibited.append("Gibbs/free-energy output is not available")
    if _has_any(text, ("全局最低", "global minimum", "构象搜索")):
        prohibited.append("global conformer minimum is not available")
    if _has_any(text, ("ts", "过渡态", "irc", "反应路径")):
        prohibited.append("TS/IRC reaction-path workflows are not available")
    if _has_any(
        text,
        ("不要频率", "不做频率", "不需要频率", "without freq", "without frequency", "no frequency"),
    ):
        prohibited.append("frequency is explicitly prohibited")
    if _has_any(text, ("附件", "图片", "文件", "路径", "input.xyz", ".inp", ".out")):
        prohibited.append("file/image molecular intake is not connected")
    hard: list[str] = []
    if method:
        hard.append(f"method={method}")
    if environment:
        hard.append(f"environment={environment}")
    parameter_evidence: dict[str, object] = {}
    for field_name, pattern in (
        ("charge", _CHARGE),
        ("multiplicity", _MULTIPLICITY),
    ):
        match = pattern.search(text)
        if match is not None:
            parameter_evidence[field_name] = {
                "origin": "current_message",
                "quote": match.group(0),
            }
    if method:
        parameter_evidence["method"] = {
            "origin": "current_message",
            "quote": method_match.group(0) if method_match is not None else method,
        }
    if environment:
        environment_match = re.search(r"水溶液|溶液|solvent|aqueous|solution", text, re.I)
        parameter_evidence["environment"] = {
            "origin": "current_message",
            "quote": environment_match.group(0) if environment_match else environment,
        }
    operations = _operations(text)
    output_spec = _output_spec(text)
    candidate_operations = _candidate_operations(text)
    proposal = proposal_from_operations(
        candidate_operations,
        goal=text,
        requested_outputs=_proposal_outputs(output_spec, candidate_operations),
        action=(
            action.value
            if action in {CalculationAction.PLAN_NEW, CalculationAction.REVISE_DRAFT}
            else CalculationAction.PLAN_NEW.value
        ),
    )
    if not candidate_operations:
        proposal = proposal.model_copy(update={"clarification_fields": ("execution_scope",)})
    return CalculationIntent(
        action=action,
        molecule_kind=molecule_kind,
        molecule_value=molecule_value,
        task_alias=_extract_task_alias(text),
        operations=operations,
        charge=charge,
        multiplicity=multiplicity,
        method=method,
        environment=environment,
        hard_constraints=tuple(hard),
        prohibited_requests=tuple(prohibited),
        output_spec=output_spec,
        requested_execution=_has_any(text, ("开始", "执行", "run it", "start it", "approve")),
        parameter_evidence=parameter_evidence,
        plan_proposal=proposal.model_dump(mode="json"),
    )


class BaselinePlanner:
    """A deterministic planner that never performs a scientific side effect."""

    adapter_id = "baseline"
    model = "deterministic-baseline-v1"

    def interpret_text(self, text: str) -> TurnInterpretation:
        stripped = text.strip()
        subrequests: list[object] = []

        # Preserve the order of an explanatory request followed by a new task.
        if (
            re.search(r"(?:再|然后|之后|and then|then)", stripped, re.I)
            and _has_any(stripped, ("区别", "什么是", "解释", "difference", "what is"))
            and _has_any(stripped, ("优化", "optimize", "算", "calculate"))
        ):
            separator = re.search(r"(?:再|然后|之后|and then|then)", stripped, re.I)
            first = stripped[: separator.start()] if separator else stripped
            second = stripped[separator.end() :] if separator else stripped
            subrequests.append(
                ChemistryQAIntent(
                    question=first.strip(" ，,；;"),
                    answer_draft=self._chemistry_answer(first),
                )
            )
            subrequests.append(_calculation(second))
        elif _has_any(stripped, ("取消", "cancel", "停止任务", "终止任务")):
            subrequests.append(_calculation(stripped, action=CalculationAction.CANCEL_TASK))
        elif _context_query_like(stripped):
            subrequests.append(
                ContextQueryIntent(
                    query=stripped,
                    task_alias=_extract_task_alias(stripped),
                    output_spec=_query_output_spec(stripped),
                    requested_display_change=_has_any(
                        stripped, ("只显示", "仅显示", "改成", "显示", "单位", "小数")
                    ),
                )
            )
        elif _question_like(stripped) and _has_any(
            stripped, ("优化", "单点", "频率", "振动", "电荷", "多重度", "r2scan", "orca", "method")
        ):
            subrequests.append(
                ChemistryQAIntent(
                    question=stripped,
                    task_alias=_extract_task_alias(stripped),
                    answer_draft=self._chemistry_answer(stripped),
                    requires_task_context=_has_any(stripped, ("本次", "这次", "刚才", "结果")),
                )
            )
        elif _has_any(
            stripped,
            (
                "优化",
                "optimize",
                "计算",
                "calculate",
                "算一下",
                "频率",
                "单点",
                "run",
                "去掉",
                "删除",
                "移除",
                "改成只做",
                "补做",
                "追加",
                "remove",
                "drop",
            ),
        ):
            edit = _has_any(
                stripped,
                ("去掉", "删除", "移除", "改成只做", "remove", "drop"),
            )
            subrequests.append(
                _calculation(
                    stripped,
                    action=CalculationAction.REVISE_DRAFT if edit else CalculationAction.PLAN_NEW,
                )
            )
        elif _has_any(stripped, ("你好", "您好", "欢迎", "hello", "hi", "thanks", "谢谢")):
            subrequests.append(
                GeneralQAIntent(
                    question=stripped,
                    answer_draft="你好！我可以帮助你规划受支持的化学计算，也可以查询已有任务或解释计算概念。",
                )
            )
        elif _has_any(stripped, ("区别", "什么是", "解释", "difference", "what is")):
            subrequests.append(
                ChemistryQAIntent(
                    question=stripped,
                    answer_draft=self._chemistry_answer(stripped),
                )
            )
        else:
            subrequests.append(
                GeneralQAIntent(
                    question=stripped, answer_draft="我已收到这条消息，但需要更具体的任务或问题。"
                )
            )
        has_plan = any(
            isinstance(item, CalculationIntent)
            and item.action in {CalculationAction.PLAN_NEW, CalculationAction.REVISE_DRAFT}
            for item in subrequests
        )
        return TurnInterpretation(
            schema_version=TURN_SCHEMA_V3
            if has_plan
            else TurnInterpretation.model_fields["schema_version"].default,
            prompt_version=PROMPT_VERSION_V3
            if has_plan
            else TurnInterpretation.model_fields["prompt_version"].default,
            subrequests=tuple(subrequests),
            source=ResponseSource.BASELINE,
        )

    def interpret(self, context: ContextSnapshot) -> TurnInterpretation:
        return self.interpret_text(context.current_message)

    @staticmethod
    def _chemistry_answer(text: str) -> str:
        if _has_any(text, ("优化", "单点", "optimization", "single point")):
            return (
                "几何优化会改变原子坐标，寻找给定方法下的局部驻点；单点计算在固定几何上评估能量。"
                "本项目会按当前目标选择 Opt、Freq 或独立 SP；独立 SP 能量不能用 Opt 末步能量替代。"
            )
        if _has_any(text, ("频率", "振动", "frequency")):
            return (
                "频率计算由 Hessian 对振动模式进行分类；虚频和刚体近零模式需要按科学策略单独解释。"
            )
        return (
            "这是化学知识解释，不会自动成为本次计算的科学 Claim；"
            "具体任务结论以已验证结果和证据为准。"
        )


__all__ = ["BaselinePlanner"]
