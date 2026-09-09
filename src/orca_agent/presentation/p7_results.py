"""Select and render P7 results from verified P5/P6 read projections.

This module is intentionally boring: it never asks a model to fill a number,
and it never derives a scientific value from two unrelated records.  P5 and
P6 services have already performed their source-chain checks before a view is
handed to the presenter.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime

from orca_agent.application.p6_service import P6RunView
from orca_agent.domain.p5 import P5Phase, P5ResultRecord
from orca_agent.domain.p6 import P6ComparabilityStatus, P6SourceResultRef
from orca_agent.domain.p7_task import (
    DeliveryRecord,
    Fulfillment,
    FulfillmentStatus,
    OutputKind,
    OutputSpec,
    OverallDeliveryStatus,
    TaskRecord,
)
from orca_agent.infrastructure.clock import SystemClock, format_utc
from orca_agent.orchestration.p7_versions import P7_POLICY_VERSION

P7_PRESENTATION_VERSION = "p7-presenter-v1"


def _dump(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _text_value(value: object, *, precision: int | None = None) -> str:
    if isinstance(value, float):
        if precision is None:
            return repr(value)
        return f"{value:.{precision}f}"
    if isinstance(value, list):
        return "[" + ", ".join(_text_value(item, precision=precision) for item in value) + "]"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if value is None:
        return "—"
    return str(value)


def _required_statuses(items: Iterable[Fulfillment]) -> tuple[Fulfillment, ...]:
    return tuple(item for item in items if item.required)


def _overall_status(items: tuple[Fulfillment, ...]) -> OverallDeliveryStatus:
    required = _required_statuses(items)
    if not required:
        return OverallDeliveryStatus.FULFILLED
    statuses = {item.status for item in required}
    if statuses == {FulfillmentStatus.PROVIDED}:
        return OverallDeliveryStatus.FULFILLED
    if FulfillmentStatus.PENDING in statuses:
        return (
            OverallDeliveryStatus.PENDING
            if all(item.status is FulfillmentStatus.PENDING for item in required)
            else OverallDeliveryStatus.PARTIAL
        )
    if any(item.status is FulfillmentStatus.PROVIDED for item in required):
        return OverallDeliveryStatus.PARTIAL
    return OverallDeliveryStatus.UNMET


class P7ResultPresenter:
    """Build immutable deliveries and deterministic human/JSON views."""

    version = P7_PRESENTATION_VERSION

    def build_delivery(
        self,
        task: TaskRecord,
        *,
        p5_view: object | None = None,
        p6_view: P6RunView | None = None,
        output_spec: OutputSpec | None = None,
        now: datetime | None = None,
    ) -> DeliveryRecord:
        spec = (
            output_spec
            or task.delivery_output_spec
            or (task.request.output_spec if task.request is not None else OutputSpec.default())
        )
        p5_results = tuple(getattr(p5_view, "results", ())) if p5_view is not None else ()
        p6_results = () if p6_view is None else tuple(p6_view.source_snapshot.results)
        fulfillments = tuple(
            self._fulfill(
                quantity.kind,
                quantity.required,
                quantity.source_selector,
                quantity.unit,
                p5_view=p5_view,
                p5_results=p5_results,
                p6_view=p6_view,
                p6_results=p6_results,
                precision=spec.precision,
            )
            for quantity in spec.quantities
        )
        p5_state = None if p5_view is None else getattr(p5_view, "state", None)
        p6_state = None if p6_view is None else p6_view.state
        source = {
            "presentation_version": self.version,
            "policy_version": P7_POLICY_VERSION,
            "task_id": task.task_id,
            "task_revision": task.revision,
            "p5_run_id": None if p5_view is None else str(p5_view.run_id),
            "p5_revision": None if p5_view is None else p5_view.revision,
            "p5_phase": None if p5_state is None else p5_state.phase.value,
            "p6_run_id": None if p6_view is None else str(p6_view.run_id),
            "p6_revision": None if p6_view is None else p6_view.revision,
            "p6_phase": None if p6_state is None else p6_state.phase.value,
            "result_ids": [str(item.record_id) for item in p5_results],
            "p6_result_ids": [str(item.result_id) for item in p6_results],
            "claim_ids": [] if p6_view is None else [str(item.claim_id) for item in p6_view.claims],
            "read_at_utc": format_utc(now or SystemClock().now_utc()),
        }
        return DeliveryRecord.create(
            delivery_id=f"delivery_{uuid.uuid4().hex}",
            task_id=task.task_id,
            delivery_version=1,
            output_spec=spec,
            source=source,
            fulfillment=fulfillments,
            overall_status=_overall_status(fulfillments),
            created_at_utc=now or SystemClock().now_utc(),
        )

    def _fulfill(
        self,
        kind: OutputKind,
        required: bool,
        selector: str | None,
        requested_unit: str | None,
        *,
        p5_view: object | None,
        p5_results: tuple[P5ResultRecord, ...],
        p6_view: P6RunView | None,
        p6_results: tuple[P6SourceResultRef, ...],
        precision: int | None,
    ) -> Fulfillment:
        if kind in {
            OutputKind.OPT_FINAL_ELECTRONIC_ENERGY,
            OutputKind.INDEPENDENT_SP_ELECTRONIC_ENERGY,
        }:
            primitive = "opt" if kind is OutputKind.OPT_FINAL_ELECTRONIC_ENERGY else "sp"
            source = self._find_source(primitive, selector, p5_results, p6_results)
            if source is None:
                return self._missing(
                    kind,
                    required,
                    p5_view,
                    reason=("独立 SP 尚未完成" if primitive == "sp" else "Opt 末步能量尚未完成"),
                )
            energy = getattr(source, "energy", None)
            token = getattr(source, "energy_token", None)
            if energy is None or token is None:
                return self._missing(kind, required, p5_view, reason="来源记录没有可交付的电子能量")
            unit = requested_unit or getattr(source, "energy_unit", None) or "Eh"
            if unit != "Eh":
                return Fulfillment(
                    kind=kind,
                    required=required,
                    status=FulfillmentStatus.UNSUPPORTED,
                    source_selector=self._source_id(source),
                    reason=f"当前只注册 {unit!r} 以外的单位转换不可用",
                )
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=self._source_id(source),
                value=energy,
                raw_value_token=token,
                unit="Eh",
                evidence=self._evidence_for_energy(p6_view, primitive),
            )

        if kind is OutputKind.VIBRATIONAL_FREQUENCIES:
            source = self._find_source("freq", selector, p5_results, p6_results)
            if source is None:
                return self._missing(kind, required, p5_view, reason="Freq 节点尚未完成")
            frequencies = tuple(getattr(source, "frequencies", ()))
            if not frequencies:
                return self._missing(kind, required, p5_view, reason="来源记录没有频率数据")
            tokens = getattr(source, "frequency_tokens", ())
            if not tokens and isinstance(source, P5ResultRecord):
                tokens = ()
            artifact = {"raw_tokens": list(tokens)} if tokens else None
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=self._source_id(source),
                value=list(frequencies),
                unit=requested_unit or getattr(source, "frequency_unit", None) or "cm^-1",
                artifact=artifact,
                evidence=self._evidence_for_frequency(p6_view),
            )

        if kind is OutputKind.OPTIMIZED_GEOMETRY:
            source = self._find_source("opt", selector, p5_results, p6_results)
            artifact_id = (
                None if source is None else getattr(source, "optimized_geometry_artifact_id", None)
            )
            if artifact_id is None:
                return self._missing(kind, required, p5_view, reason="优化 XYZ 产物尚未完成")
            artifact = {"artifact_id": str(artifact_id), "role": "optimized_xyz"}
            if p6_view is not None:
                for item in p6_view.source_snapshot.artifacts:
                    if item.artifact_id == artifact_id:
                        artifact.update(
                            {
                                "content_hash": item.content_hash,
                                "relative_path": item.relative_path,
                                "owner_run_id": str(item.owner_run_id),
                            }
                        )
                        break
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=self._source_id(source),
                value={"artifact_id": str(artifact_id)},
                artifact=artifact,
                evidence=self._evidence_for_geometry(p6_view),
            )

        if kind is OutputKind.LOCAL_MINIMUM_SUPPORT:
            if p6_view is None or p6_view.assessment is None:
                return self._missing(kind, required, p5_view, reason="P6 科学评估尚未完成")
            assessment = p6_view.assessment
            status = FulfillmentStatus.PROVIDED
            return Fulfillment(
                kind=kind,
                required=required,
                status=status,
                source_selector=f"p6.assessment:{assessment.assessment_id}",
                value=assessment.minimum_status.value,
                reason=assessment.minimum_reason,
                evidence=tuple(str(item) for item in assessment.evidence_ids),
            )

        if kind is OutputKind.EXECUTION_STATUS:
            if p5_view is None:
                return self._missing(kind, required, p5_view, reason="P5 执行尚未建立")
            state = p5_view.state
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=f"p5.state:{p5_view.run_id}:{p5_view.revision}",
                value={
                    "run_id": str(p5_view.run_id),
                    "revision": p5_view.revision,
                    "phase": state.phase.value,
                    "status": state.status.value,
                    "current_node_id": state.current_node_id,
                    "last_outcome_code": state.last_outcome_code,
                    "last_error_code": state.last_error_code,
                },
            )

        if kind is OutputKind.SCIENTIFIC_STATUS:
            if p6_view is None:
                return self._missing(kind, required, p5_view, reason="P6 科学评估尚未建立")
            assessment = p6_view.assessment
            if assessment is None:
                return self._missing(kind, required, p5_view, reason="P6 科学评估尚未完成")
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=f"p6.assessment:{assessment.assessment_id}",
                value={
                    "assessment_id": str(assessment.assessment_id),
                    "minimum_status": assessment.minimum_status.value,
                    "integrity_verified": assessment.integrity_verified,
                    "warnings": list(assessment.warnings),
                    "limitations": list(assessment.limitations),
                },
                evidence=tuple(str(item) for item in assessment.evidence_ids),
            )

        if kind is OutputKind.REPORT:
            if p6_view is None or p6_view.report_manifest is None:
                return self._missing(kind, required, p5_view, reason="P6 报告尚未生成")
            manifest = p6_view.report_manifest
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=f"p6.report:{manifest.report_manifest_id}",
                value={
                    "report_manifest_id": str(manifest.report_manifest_id),
                    "markdown_artifact_id": str(manifest.markdown_artifact_id),
                    "json_artifact_id": str(manifest.json_artifact_id),
                },
                artifact={
                    "markdown_artifact_id": str(manifest.markdown_artifact_id),
                    "markdown_hash": manifest.markdown_hash,
                    "json_artifact_id": str(manifest.json_artifact_id),
                    "json_hash": manifest.json_hash,
                    "manifest_hash": manifest.manifest_hash,
                },
                evidence=tuple(str(item) for item in manifest.evidence_ids),
            )

        if kind is OutputKind.EXISTING_ENERGY_DIFFERENCE:
            if p6_view is None:
                return self._missing(kind, required, p5_view, reason="没有关联的 P6 可比性评估")
            comparison = next(
                (
                    item
                    for item in p6_view.comparisons
                    if item.status is P6ComparabilityStatus.COMPATIBLE
                ),
                None,
            )
            if comparison is None:
                return Fulfillment(
                    kind=kind,
                    required=required,
                    status=FulfillmentStatus.NOT_AVAILABLE,
                    reason="没有通过 P6 可比性校验的已有能量差值",
                )
            return Fulfillment(
                kind=kind,
                required=required,
                status=FulfillmentStatus.PROVIDED,
                source_selector=f"p6.comparability:{comparison.record_id}",
                value=comparison.delta_energy,
                raw_value_token=comparison.delta_energy_token,
                unit=comparison.unit,
                reason="E_candidate - E_reference；仅使用已有 P6 可比性记录",
            )

        return Fulfillment(
            kind=kind,
            required=required,
            status=FulfillmentStatus.UNSUPPORTED,
            reason="OutputKind 未注册",
        )

    @staticmethod
    def _source_id(source: object) -> str:
        return (
            f"p5.result:{source.record_id}"
            if isinstance(source, P5ResultRecord)
            else f"p6.result:{source.result_id}"
        )

    @staticmethod
    def _find_source(
        primitive: str,
        selector: str | None,
        p5_results: tuple[P5ResultRecord, ...],
        p6_results: tuple[P6SourceResultRef, ...],
    ) -> P5ResultRecord | P6SourceResultRef | None:
        candidates: tuple[P5ResultRecord | P6SourceResultRef, ...] = p6_results or p5_results
        if selector:
            selected = tuple(
                item
                for item in candidates
                if selector
                in {
                    primitive,
                    getattr(item, "primitive", None),
                    str(getattr(item, "record_id", getattr(item, "result_id", ""))),
                }
            )
            if selected:
                return selected[-1]
        matches = tuple(
            item
            for item in candidates
            if getattr(item.primitive, "value", item.primitive) == primitive
        )
        return matches[-1] if matches else None

    @staticmethod
    def _missing(
        kind: OutputKind, required: bool, p5_view: object | None, *, reason: str
    ) -> Fulfillment:
        phase = None if p5_view is None else getattr(getattr(p5_view, "state", None), "phase", None)
        terminal = phase in {P5Phase.FAILED, P5Phase.CANCELLED} if phase is not None else False
        return Fulfillment(
            kind=kind,
            required=required,
            status=FulfillmentStatus.NOT_AVAILABLE if terminal else FulfillmentStatus.PENDING,
            reason=reason,
        )

    @staticmethod
    def _evidence_for_energy(p6_view: P6RunView | None, primitive: str) -> tuple[str, ...]:
        if p6_view is None:
            return ()
        return tuple(
            str(item.evidence_id)
            for item in p6_view.evidence
            if item.evidence_type.value == "electronic_energy"
            and (primitive in item.quantity or primitive in str(item.value))
        )

    @staticmethod
    def _evidence_for_frequency(p6_view: P6RunView | None) -> tuple[str, ...]:
        if p6_view is None:
            return ()
        return tuple(
            str(item.evidence_id)
            for item in p6_view.evidence
            if item.evidence_type.value == "vibrational_frequency"
        )

    @staticmethod
    def _evidence_for_geometry(p6_view: P6RunView | None) -> tuple[str, ...]:
        if p6_view is None:
            return ()
        return tuple(
            str(item.evidence_id)
            for item in p6_view.evidence
            if item.evidence_type.value == "minimum_check"
        )

    def render(self, delivery: DeliveryRecord, *, format: str = "json") -> dict[str, object] | str:
        if format == "json":
            return delivery.model_dump(mode="json")
        if format != "md":
            raise ValueError("P7 result format must be json or md")
        lines = [
            "# P7 Result",
            "",
            f"- Delivery: `{delivery.delivery_id}`",
            f"- Task: `{delivery.task_id}`",
            f"- Overall: `{delivery.overall_status.value}`",
            "",
            "| Output | Status | Value | Unit | Source |",
            "|---|---|---|---|---|",
        ]
        for item in delivery.fulfillment:
            lines.append(
                "| {kind} | {status} | {value} | {unit} | {source} |".format(
                    kind=item.kind.value,
                    status=item.status.value,
                    value=_text_value(item.value, precision=delivery.output_spec.precision),
                    unit=item.unit or "—",
                    source=item.source_selector or "—",
                )
            )
        notes = [item.reason for item in delivery.fulfillment if item.reason]
        if notes:
            lines.extend(["", "## Notes", "", *[f"- {note}" for note in notes]])
        return "\n".join(lines) + "\n"


__all__ = ["P7ResultPresenter", "P7_PRESENTATION_VERSION"]
