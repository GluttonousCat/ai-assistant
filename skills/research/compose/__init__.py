# -*- encoding: utf-8 -*-
"""skills.research.compose — Stage 2 content generation (explanation layer + judgment layer).

generator.py       T06 draft generator: evidence pack -> ten-chapter draft.json
                    (LLM chapter by chapter, non-streaming); the prompt library is
                    imported directly from T05 (prompts_chapters)
citation_check.py  T06 citation checker (pure logic, offline testable): numeric-sentence
                    heuristics / ghost citations / opinion-sentence conventions
figures.py         T07 figure factory (a separate task, not part of this package initially)
"""
