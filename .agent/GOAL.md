# GOAL

Audit the post-reset `picast` codebase (`app/`, `modules/`, `service/`, `packaging/`, `tools/`, `.vscode/`, `.github/`) for correctness, maintainability, and removal of hacked-in or test/probing-only code, then plan and execute approved fixes and refactorings while tracking all changes and reasoning in `.agent/`.

## Scope Constraints
1. Suggest and document all proposed removals, fixes, and refactorings before modifying application source files.
2. Verify all findings strictly against the post-reset active files (`.archive/` contains only empty directories / zero files).
3. Remove features, flags, scripts, and dead branches that exist solely for manual testing or probing.
4. Preserve legitimate operator diagnostics (`picast doctor`, `picast status`) and automated unit tests (`ctest`, `pytest`).
