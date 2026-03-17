# Unreal AI Safety Rules

## Scope

These rules constrain AI-driven work performed through:
- [use-unrealhub](C:\Users\alain\Documents\Playground\UnrealMCPHub\skills\use-unrealhub\SKILL.md)

They are intended to reduce project pollution in Unreal projects where many important assets are binary and hard to diff directly.

## Core Principles

1. Prefer bounded changes over broad edits.
2. Prefer new sandbox assets over modifying shared assets.
3. Prefer explicit review points over silent autonomous changes.
4. Treat binary assets as high-risk unless scope is narrowly controlled.

## Default Allowed Scope

Allowed by default:
- read project status and logs
- inspect tool availability
- create or modify assets under a sandbox content root
- use a dedicated test map
- run validation commands
- summarize changes

Not allowed by default:
- modify production maps
- delete existing shared assets
- rename shared assets
- change project-wide config
- change plugins or engine settings
- modify shared base blueprints
- perform large-scale migration or refactor work

## Recommended Sandbox Boundaries

Default content root:
- `/Game/__Sandbox/`

Default test map:
- `/Game/__Sandbox/Maps/AI_TestMap`

Optional restricted feature roots:
- `/Game/UI/Prototype/`
- `/Game/Gameplay/Prototype/`

## Change Control Rules

### Rule 1: Plan Before Edit

Before any write action, the agent must state:
- intended target paths
- intended asset or code types
- validation plan

### Rule 2: Stay Inside Approved Paths

The agent may only modify:
- paths explicitly approved for the task

If the task requires leaving the approved path, stop and escalate to review.

### Rule 3: Do Not Delete By Default

Do not delete assets, files, or maps unless the task explicitly requires deletion and a reviewer has approved that operation.

### Rule 4: Treat Shared Assets As High Risk

High-risk assets include:
- shared blueprint parents
- main HUD or menu widgets
- production maps
- data assets referenced by multiple systems
- project settings and config files

These require explicit approval and post-change review.

### Rule 5: Prefer Additive Work

Prefer:
- creating a new asset
- subclassing an existing blueprint
- adding a prototype widget

Over:
- changing a shared base
- rewriting a shared blueprint
- replacing an in-use asset

### Rule 6: Validate After Every Task

At minimum, validate one or more of:
- blueprint compilation
- map load
- PIE start
- expected asset creation
- log sanity

### Rule 7: Always Produce A Change Summary

Every editing task must end with:
- created assets
- modified assets or files
- validation performed
- known limitations
- follow-up recommendation

## Binary Asset Audit Strategy

Because `.uasset` and `.umap` files are hard to diff directly, audit the work through:

- path-level scope control
- asset list changes
- reference-impact review
- validation logs
- task-level operation summaries

In practice, this means:
- review what changed
- review where it changed
- review whether it loads and runs
- review whether shared references were touched

Do not rely only on raw binary diffs.

## Human Review Required

Human review is mandatory for:
- changes outside sandbox
- production map edits
- project config edits
- plugin or build setting changes
- C++ module changes
- shared asset changes
- migrations or large batch edits

## Suggested Review Checklist

- Did the task stay inside approved scope?
- Were only approved maps or directories touched?
- Were any shared assets changed?
- Did validation actually run?
- Is the change summary complete enough to audit?
- Can the result be merged or should it remain sandbox-only?

## Failure Handling

If validation fails:
- stop broadening the scope
- gather logs
- summarize the failure
- propose the smallest next corrective step

Do not keep retrying the same destructive or high-risk operation without a new plan.
