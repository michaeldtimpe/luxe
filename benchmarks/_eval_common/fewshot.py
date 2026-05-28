"""Deterministic few-shot sampling + canonical exemplar sets."""
from __future__ import annotations

import random
from typing import Sequence, TypeVar

T = TypeVar("T")


def deterministic_sample(items: Sequence[T], k: int, seed: int) -> list[T]:
    """Reproducible sample of `k` items from `items`.

    Same (items, k, seed) ⇒ same output across runs. Used for selecting
    few-shot exemplars from a dev split with a fixed seed.
    """
    rng = random.Random(seed)
    return rng.sample(list(items), min(k, len(items)))


# Canonical 8-shot CoT exemplars from Wei et al. 2022 (Table 20).
# https://arxiv.org/abs/2201.11903
# DO NOT EDIT — these are the published reference set. Changing any of
# them is a benchmark_protocol_version bump.
GSM8K_8SHOT_EXEMPLARS: tuple[tuple[str, str], ...] = (
    (
        "There are 15 trees in the grove. Grove workers will plant trees in the grove today. After they are done, there will be 21 trees. How many trees did the grove workers plant today?",
        "There are 15 trees originally. Then there were 21 trees after some more were planted. So there must have been 21 - 15 = 6. The answer is 6.",
    ),
    (
        "If there are 3 cars in the parking lot and 2 more cars arrive, how many cars are in the parking lot?",
        "There are originally 3 cars. 2 more cars arrive. 3 + 2 = 5. The answer is 5.",
    ),
    (
        "Leah had 32 chocolates and her sister had 42. If they ate 35, how many pieces do they have left in total?",
        "Originally, Leah had 32 chocolates. Her sister had 42. So in total they had 32 + 42 = 74. After eating 35, they had 74 - 35 = 39. The answer is 39.",
    ),
    (
        "Jason had 20 lollipops. He gave Denny some lollipops. Now Jason has 12 lollipops. How many lollipops did Jason give to Denny?",
        "Jason started with 20 lollipops. Then he had 12 after giving some to Denny. So he gave Denny 20 - 12 = 8. The answer is 8.",
    ),
    (
        "Shawn has five toys. For Christmas, he got two toys each from his mom and dad. How many toys does he have now?",
        "Shawn started with 5 toys. If he got 2 toys each from his mom and dad, then that is 4 more toys. 5 + 4 = 9. The answer is 9.",
    ),
    (
        "There were nine computers in the server room. Five more computers were installed each day, from monday to thursday. How many computers are now in the server room?",
        "There were originally 9 computers. For each of 4 days, 5 more computers were added. So 5 * 4 = 20 computers were added. 9 + 20 = 29. The answer is 29.",
    ),
    (
        "Michael had 58 golf balls. On tuesday, he lost 23 golf balls. On wednesday, he lost 2 more. How many golf balls did he have at the end of wednesday?",
        "Michael started with 58 golf balls. After losing 23 on tuesday, he had 58 - 23 = 35. After losing 2 more, he had 35 - 2 = 33 golf balls. The answer is 33.",
    ),
    (
        "Olivia has $23. She bought five bagels for $3 each. How much money does she have left?",
        "Olivia had 23 dollars. 5 bagels for 3 dollars each will be 5 x 3 = 15 dollars. So she has 23 - 15 dollars left. 23 - 15 is 8. The answer is 8.",
    ),
)


def build_gsm8k_8shot_prompt(question: str) -> str:
    """Render the canonical 8-shot CoT prompt with `question` appended."""
    parts: list[str] = []
    for q, a in GSM8K_8SHOT_EXEMPLARS:
        parts.append(f"Q: {q}\nA: {a}\n\n")
    parts.append(f"Q: {question}\nA:")
    return "".join(parts)
