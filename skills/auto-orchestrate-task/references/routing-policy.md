# Routing policy

The planner chooses the execution shape before choosing individual workers.

| Shape | Use when | Worker policy |
| --- | --- | --- |
| `direct` | One coherent execution path | Terra for routine work; Sol for novel, core, or high-risk work |
| `orchestrated` | Independent tasks have separate acceptance criteria | At most three non-Ultra workers per wave |
| `native-ultra` | Hard subtasks are strongly coupled and need continuous replanning | Exactly one Sol Ultra or Terra Ultra worker, alone in its wave |

Reasoning levels `low` through `max` represent increasing single-agent depth. `ultra` is reserved for automatic native delegation, not used as a generic retry step.

Use Terra Ultra for coordination-heavy everyday work with clear product boundaries. Use Sol Ultra for frontier, architectural, ambiguous, or high-risk coordination. Environment and permission failures never justify a model escalation.

On Windows, executable `access=read` tasks use a disposable snapshot by default. The original workspace is not their current directory; any snapshot content change blocks cleanup and is preserved as evidence. Parallel `access=write` tasks still require separate Git worktrees.
