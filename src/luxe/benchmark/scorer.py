"""Quality scoring — checks pipeline output against ground truth expectations."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from luxe.benchmark.tasks import BenchmarkTask, GroundTruth
from luxe.pipeline.model import PipelineRun


@dataclass
class TaskScore:
    task_id: str = ""
    config_name: str = ""
    findings_detected: list[str] = field(default_factory=list)
    findings_missed: list[str] = field(default_factory=list)
    files_touched_correct: list[str] = field(default_factory=list)
    files_touched_missed: list[str] = field(default_factory=list)
    detection_rate: float = 0.0
    total_findings_in_report: int = 0
    has_false_positives: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "config_name": self.config_name,
            "detection_rate": round(self.detection_rate, 3),
            "findings_detected": self.findings_detected,
            "findings_missed": self.findings_missed,
            "files_touched_correct": self.files_touched_correct,
            "files_touched_missed": self.files_touched_missed,
            "total_findings_in_report": self.total_findings_in_report,
            "notes": self.notes,
        }


def score_run(
    run: PipelineRun,
    task: BenchmarkTask,
    config_name: str = "",
) -> TaskScore:
    """Score a pipeline run against ground truth expectations."""
    score = TaskScore(task_id=task.id, config_name=config_name)
    report = (run.final_report or "").lower()
    all_output = "\n".join([
        run.architect_result or "",
        *[s.result_text or "" for s in run.subtasks],
        run.validator_result or "",
        run.synthesizer_result or "",
    ]).lower()

    gt = task.ground_truth

    for expected in gt.expected_findings:
        keywords = _extract_keywords(expected)
        if _fuzzy_match(keywords, report) or _fuzzy_match(keywords, all_output):
            score.findings_detected.append(expected)
        else:
            score.findings_missed.append(expected)

    for expected_file in gt.expected_files_touched:
        if expected_file.lower() in all_output:
            score.files_touched_correct.append(expected_file)
        else:
            score.files_touched_missed.append(expected_file)

    total_expected = len(gt.expected_findings)
    if total_expected > 0:
        score.detection_rate = len(score.findings_detected) / total_expected

    score.total_findings_in_report = _count_findings(report)

    if gt.min_findings > 0 and score.total_findings_in_report < gt.min_findings:
        score.notes.append(
            f"Below minimum findings: {score.total_findings_in_report} < {gt.min_findings}"
        )

    return score


def _extract_keywords(description: str) -> list[str]:
    """Pull key terms from a ground-truth description for fuzzy matching."""
    stop_words = {"in", "the", "a", "an", "of", "for", "to", "via", "with", "and", "or", "is", "on", "no", "not"}
    words = re.findall(r'[a-z_][a-z0-9_.]+', description.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]


def _fuzzy_match(keywords: list[str], text: str) -> bool:
    """Check if enough keywords appear in the text to consider it a match."""
    if not keywords:
        return False
    matches = sum(1 for kw in keywords if kw in text)
    threshold = max(2, len(keywords) * 0.5)
    return matches >= threshold


def _count_findings(report: str) -> int:
    """Rough count of findings in a report based on common patterns."""
    patterns = [
        r'^\s*[-*]\s+',        # bullet points
        r'^\s*\d+\.\s+',       # numbered lists
        r'`[^`]+:\d+`',        # file:line citations
        r'[a-z_]+\.[a-z]+:\d+',  # file.ext:line references
    ]
    lines = report.splitlines()
    count = 0
    for line in lines:
        for pat in patterns:
            if re.search(pat, line):
                count += 1
                break
    return count
