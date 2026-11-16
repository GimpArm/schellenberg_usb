# Agent Instructions & Repository Guidelines

Follow these rules strictly when modifying code, managing git history, testing, or writing documentation in this repository.

## 1. Git & Commit Discipline
- **Do NOT commit after every file edit or incremental step.**
- Group related changes into logical, atomic commits. A single feature, bug fix, or refactoring task should result in **1 to 3 meaningful commits**, never dozens or hundreds of micro-commits.
- Only commit when a logical unit of work is complete, tested, and verified.
- If you have made multiple small work-in-progress commits on a feature branch, squash or amend them into clean, logical commits before finishing your task or opening a pull request.
- Write clear, concise commit messages summarizing *what* changed and *why*.

## 2. Code Commenting Standards
- **Keep inline code comments minimal, short, and to the point.**
- Do not write tutorial-style explanations, narrate obvious code behavior, or leave multi-paragraph essays inside source files.
- Only add inline comments to clarify **why** a non-obvious decision, workaround, or complex business rule exists—never to explain *what* the syntax is doing.
- Use standard Python docstrings (PEP 257 compliant, following Google or NumPy style) only for public modules, classes, and functions, keeping descriptions brief (1–2 sentences plus parameters/returns if necessary). Avoid verbose or multi-page docstrings.

## 3. Documentation (`docs/`)
- **Never bloat source code with extensive architectural notes, design rationales, or setup guides.**
- Whenever a change requires detailed explanation, architectural context, configuration guides, or operational notes, create or update a Markdown file in the root `/docs` directory (e.g., `docs/architecture.md`, `docs/sync-pipeline.md`).
- If relevant, leave a single-line pointer in the code referencing the documentation file (e.g., `# See docs/sync-pipeline.md for retry state machine details.`).

## 4. Testing Philosophy & Unit Tests
- **Prioritize meaningful, real-world simulating tests over metric padding.**
- **Do NOT chase 100% branch or line coverage.** Blindly chasing arbitrary coverage thresholds results in brittle, low-value tests that assert implementation details rather than actual functionality.
- Write tests using `pytest` that focus on core business logic, critical paths, and realistic usage scenarios.
- Avoid over-mocking. Do not mock internal components to the point where tests merely verify that your mocks talk to each other; test real inputs, outputs, and side effects where appropriate.
- Keep the test suite lean, fast, and maintainable.