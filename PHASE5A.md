PHASE 8
────────────
Handoff Lifecycle
□ CHANGELOG.md
□ checkpoint lifecycle
□ history handling
□ validation
□ tests

CHECKPOINT 8
→ Semua test pass
→ Tidak ada regression


PHASE 9
────────────
Production Provider & Configuration
□ provider expansion
□ model configuration
□ environment-based API keys
□ provider selection
□ error handling
□ tests

CHECKPOINT 9
→ Semua provider/config tests pass
→ Security regression pass


PHASE 10
────────────
Final Audit & Release
□ security audit
□ full regression
□ installer audit
□ uninstaller audit
□ CLI UX
□ README
□ LICENSE
□ clean-install test
□ release readiness

CHECKPOINT 10
→ MVP READY
→ Tidak ada perubahan source yang belum diaudit
→ Siap dibuat repository GitHubPHASE 7 — PERSISTENCE & SAFE COMMIT

You are implementing Phase 7 of the Handoff Agent project.

Project:
  /root/handoff-agent

Read the existing implementation and tests first.
Do not assume the previous phase reports are fully accurate; verify the actual repository state.

OBJECTIVE

Activate persistence of the generated handoff into:

  docs/HANDOFF.md

The generated HANDOFF.md represents the CURRENT CHECKPOINT ONLY.
It must be overwritten on each successful handoff generation, not appended as history.

Phase 7 must preserve all security guarantees from Phases 1–6.

ARCHITECTURE

Keep this flow:

  CLI
    ↓
  Detector
    ↓
  GitInspector
    ↓
  SecurityFilter
    ↓
  ContextBuilder
    ↓
  FullContext
    ↓
  PromptBuilder
    ↓
  ProviderFactory
    ↓
  Selected Provider
    ↓
  Generated Handoff
    ↓
  Safe Persistence
    ↓
  Show Diff
    ↓
  Optional Commit

IMPORTANT:
Provider adapters must remain isolated.

Providers MUST NOT:
- access the filesystem directly
- execute Git commands
- inspect environment variables except through their existing controlled API-key mechanism
- read .env or credentials
- modify project files
- commit or push
- discover arbitrary URLs

==================================================
1. HANDOFF FILE
==================================================

Implement safe persistence for:

  docs/HANDOFF.md

Requirements:

- Create docs/ if necessary.
- Create HANDOFF.md if it does not exist.
- Overwrite HANDOFF.md when generating a new checkpoint.
- Never append generated checkpoints to the existing file.
- Only this exact project-relative path may be written.
- Resolve and verify the final path is contained inside the detected project root.
- Never follow a path that escapes the project root through symlinks.
- Never write to arbitrary paths supplied by an AI/provider.
- The generated AI response must be treated as CONTENT, never as a filesystem path or command.
- Do not modify any source code or other project files.
- Do not modify README, package files, configuration, source files, tests, etc.

Prefer safe/atomic persistence if practical.

==================================================
2. CLI BEHAVIOR
==================================================

Preserve existing behavior from Phase 6.

Normal generation:

  handoff

should:

1. Detect project.
2. Inspect Git.
3. Build secure FullContext.
4. Build prompt.
5. Generate using selected provider.
6. Persist result to:

     docs/HANDOFF.md

7. Show what changed.
8. Return success.

Dry run:

  handoff --dry-run

MUST:

- build context
- build prompt
- NOT call the provider
- NOT require API key
- NOT write HANDOFF.md
- NOT modify any project file
- NOT commit
- NOT push

==================================================
3. DIFF DISPLAY
==================================================

After successful persistence, show the user the relevant change.

The implementation must make it clear whether:

- HANDOFF.md was created
- HANDOFF.md was modified
- HANDOFF.md was unchanged

Do not dump unrelated project diffs unnecessarily.

Do not expose secrets while displaying the diff.

The generated content itself must not cause arbitrary terminal commands or shell execution.

==================================================
4. SAFE COMMIT
==================================================

Implement the existing --commit option.

Example:

  handoff --commit

Behavior:

1. Generate HANDOFF.md.
2. Persist it.
3. Show the HANDOFF.md change.
4. Only if --commit is explicitly supplied, create a Git commit.

CRITICAL:

The commit operation MUST stage ONLY:

  docs/HANDOFF.md

Never use:

  git add .
  git add -A
  git add --all
  git add '*'
  git add -u

Use an explicitly constrained operation equivalent to:

  git add -- docs/HANDOFF.md

Then commit with a deterministic safe message, for example:

  docs: update handoff checkpoint

Do not stage or commit unrelated user changes.

If unrelated files are already modified/untracked, they MUST remain untouched and unstaged.

==================================================
5. GIT SAFETY ARCHITECTURE
==================================================

Existing git_helper.py intentionally blocks mutation commands.

Do NOT bypass the safety architecture with arbitrary subprocess calls.

Extend the Git abstraction with narrowly scoped, explicit mutation methods if necessary.

For example, use dedicated methods conceptually equivalent to:

  stage_handoff()
  commit_handoff()

These methods must enforce:

- repository containment
- exact target path docs/HANDOFF.md
- no arbitrary path arguments
- no shell=True
- argument-list subprocess execution
- no push
- no reset
- no clean
- no checkout
- no restore
- no switch
- no merge
- no rebase
- no stash
- no fetch
- no pull
- no arbitrary Git command execution

The only new Git mutations allowed in Phase 7 are:

  git add -- docs/HANDOFF.md
  git commit ...

Push remains FORBIDDEN.

==================================================
6. COMMIT FAILURE SAFETY
==================================================

Handle commit failures safely.

Examples:

- Git user identity missing
- commit fails
- repository state changes unexpectedly
- HANDOFF.md cannot be staged

Requirements:

- nonzero exit code on failure
- clear safe error
- no secrets in error output
- never fall back to another Git command
- never automatically push
- never reset/clean/revert the repository
- never delete HANDOFF.md
- never modify unrelated files

If staging succeeds but commit fails, do not attempt destructive cleanup.

==================================================
7. EXISTING USER CHANGES
==================================================

This is critical.

The project may already contain:

- modified files
- staged files
- untracked files

The Handoff Agent MUST NOT overwrite or stage them.

Tests must verify:

Before:

  modified source file
  staged unrelated file
  untracked unrelated file

After:

  only docs/HANDOFF.md is added by the agent.

The agent must not unstage existing staged files.

==================================================
8. SECURITY
==================================================

Preserve Phase 3 and Phase 4 protections.

Never expose:

- API keys
- access tokens
- passwords
- private keys
- .env contents
- credentials
- secrets

Do not put secret values into:

- HANDOFF.md
- stdout
- stderr
- exceptions
- logs
- commit messages
- test output

The generated HANDOFF content comes from the provider, so add appropriate safeguards before persistence if necessary.

Do not attempt to discover secrets from the environment.

==================================================
9. HANDOFF CONTENT
==================================================

Do not invent a new historical log system.

HANDOFF.md is the current state/checkpoint.

It should contain useful information generated from the actual FullContext, such as:

- current project state
- current Git state
- work completed
- relevant files
- current implementation status
- known issues
- next recommended step
- important context for the next AI

Do not claim work was completed merely because the model says so if the available repository evidence contradicts it.

The existing prompt architecture should remain responsible for generating the content.

Do not create CHANGELOG.md as part of Phase 7 unless it is already required by existing code/tests.
Historical changelog functionality can be handled in a later phase.

==================================================
10. TESTS
==================================================

Add comprehensive tests.

At minimum cover:

A. HANDOFF creation
- docs/ does not exist
- HANDOFF.md created correctly

B. HANDOFF update
- existing HANDOFF.md overwritten
- old content removed
- new content persisted

C. containment
- exact docs/HANDOFF.md allowed
- traversal rejected
- absolute external path rejected
- symlink escape rejected

D. source protection
- source files unchanged
- unrelated files unchanged

E. dry-run
- no HANDOFF.md write
- no provider call
- no API key requirement
- no Git mutation

F. diff behavior
- new file detected
- modified file detected
- unchanged content handled safely

G. commit
- --commit stages only docs/HANDOFF.md
- commit message is safe/deterministic
- unrelated modified files are not staged
- unrelated staged files remain staged
- unrelated untracked files remain untouched

H. forbidden Git operations
Verify Phase 1–6 forbidden operations remain forbidden.

I. push protection
- no git push
- no network Git operation

J. failures
- write failure
- staging failure
- commit failure
- non-Git directory
- malformed provider response if applicable

K. secret leakage
Use fake secrets in tests and verify they never appear in:

- stdout
- stderr
- exception messages
- commit message
- HANDOFF.md where the security layer can prevent them

L. regression
Run the complete test suite.

==================================================
11. DEPENDENCY POLICY
==================================================

Do not add dependencies unless absolutely necessary.

Prefer Python standard library.

Do not introduce requests/httpx/SDKs just for Phase 7.

==================================================
12. DOCUMENTATION
==================================================

Update documentation only where necessary to accurately describe Phase 7 behavior.

Do not modify unrelated documentation.

Document:

- normal `handoff`
- `handoff --dry-run`
- `handoff --commit`
- HANDOFF.md persistence
- commit safety
- push prohibition

==================================================
13. FINAL VALIDATION
==================================================

Before reporting completion:

1. Inspect changed files.
2. Run the complete test suite.
3. Verify no source files were accidentally modified.
4. Verify no arbitrary Git mutation path exists.
5. Verify --commit stages ONLY docs/HANDOFF.md.
6. Verify push is impossible through the Phase 7 Git interface.
7. Verify dry-run performs zero writes.
8. Verify API keys/secrets are not leaked.
9. Verify existing user changes are preserved.

Do NOT create a Git commit yourself merely because the implementation is finished.

Do NOT push anything.

==================================================
14. FINAL REPORT
==================================================

Report:

- files created
- files modified
- persistence implementation
- CLI behavior
- diff behavior
- commit behavior
- exact Git safety guarantees
- security verification
- tests passed/failed
- remaining limitations

Do not claim PASS unless the tests and repository state actually support it.

Do not modify anything outside the Phase 7 scope.Phase 4 APPROVED.

Audit:

- 332/332 tests passed.
- Phase 1–3 regression: PASS.
- SecurityFilter tetap menjadi mandatory security boundary.
- FullContext immutable dan structured.
- Tracked/untracked files melewati SecurityFilter.
- Context limits enforced.
- Deterministic ordering PASS.
- Symlink containment PASS.
- No secret/environment inspection.
- No Git mutation.
- No dependencies added.

Proceed to Phase 5A — Provider Architecture + First Provider (Claude).

Critical Security Boundary

Pipeline:

Project Detector
      ↓
Git Inspector
      ↓
SecurityFilter
      ↓
ContextBuilder
      ↓
FullContext
      ↓
Prompt Builder
      ↓
ProviderAdapter
      ↓
AI

Providers MUST NOT:

- access filesystem;
- execute project commands;
- execute Git commands;
- inspect/dump environment;
- access ".env";
- access credentials;
- modify repository.

Provider hanya menerima "FullContext" dan/atau prompt yang sudah dibuat.

---

1. Provider Abstraction

Gunakan:

"src/handoff_agent/providers/base.py"

Pertahankan interface sederhana:

class ProviderAdapter(ABC):
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate(self, context: FullContext, prompt: str) -> str:
        ...

    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        ...

Boleh memperbaiki interface jika diperlukan, tetapi tetap provider-agnostic.

Jangan memasukkan konsep khusus Claude/OpenAI ke base class.

---

2. Provider Registry / Factory

Buat registry/factory sederhana.

Tugas:

- provider name → adapter
- validasi provider
- instantiate provider
- unknown provider → clear error
- no automatic fallback

Selection:

--provider X
    ↓
explicit provider

otherwise
    ↓
config.default_provider

Jangan fallback otomatis.

---

3. Claude Provider

Implement satu provider nyata: Claude.

Buat misalnya:

"src/handoff_agent/providers/claude.py"

Model HARUS configurable.

Jangan hardcode model ID.

Contoh:

{
  "default_provider": "claude",
  "providers": {
    "claude": {
      "model": "...",
      "api_key_env": "ANTHROPIC_API_KEY"
    }
  }
}

Schema boleh diperbaiki jika lebih baik.

API key:

- tidak boleh hardcode;
- tidak boleh disimpan di repository;
- tidak boleh disimpan di config;
- tidak boleh dicetak;
- tidak boleh dilog;
- tidak boleh masuk exception.

API key hanya dibaca dari environment variable saat request diperlukan.

Jangan enumerate seluruh environment.

Jika key tidak ada, error aman:

"ANTHROPIC_API_KEY is not configured"

---

4. HTTP Implementation

Prioritaskan Python standard library.

Jangan menambahkan:

- requests
- httpx
- Anthropic SDK
- dependency besar lain

kecuali ada alasan teknis yang benar-benar diperlukan.

Jika dependency dibutuhkan:

- jelaskan alasannya;
- tambahkan seminimal mungkin;
- gunakan hanya di venv;
- tambahkan test.

---

5. Network Boundary

Phase 5A adalah fase pertama yang boleh melakukan external AI request.

Hanya "ClaudeProvider" yang boleh melakukan network request.

Tidak boleh:

- fetch Git remote;
- telemetry;
- arbitrary URL;
- network call lain.

Endpoint Claude harus configurable hanya jika memang diperlukan, tetapi jangan membuat arbitrary URL capability tanpa alasan.

Request hanya boleh mengirim prompt/context yang memang dimaksudkan untuk Claude.

---

6. Prompt Builder

Buat:

"src/handoff_agent/prompt_builder.py"

Prompt Builder hanya mengubah "FullContext" menjadi prompt.

Tidak boleh:

- filesystem access;
- Git command;
- environment access;
- secret discovery;
- file scanning.

Output harus deterministic.

Pisahkan dengan jelas:

- system instructions;
- repository context.

Safe source content dari "FullContext.files[].content" BOLEH masuk prompt.

Secret yang sudah ditolak SecurityFilter tidak boleh muncul.

---

7. FullContext Serialization

Pertahankan:

"FullContext.to_dict()"

sebagai metadata serialization yang tidak menyertakan file content.

Namun Prompt Builder boleh membaca:

"FullContext.files[].content"

karena content tersebut sudah lolos SecurityFilter.

Jangan menghapus semua source content dari prompt.

---

8. Tests

Buat test untuk:

Provider abstraction

- ABC cannot instantiate
- Claude satisfies interface
- provider name

Registry

- known provider
- unknown provider
- explicit provider
- default provider
- no fallback

Configuration

- configurable model
- configurable API key env name
- missing key safe error
- API key never appears in output

Prompt Builder

- deterministic output
- project metadata
- Git metadata
- safe file content
- excluded content absent
- no filesystem access

Claude HTTP

Mock/fake transport.

Jangan melakukan real API request.

Test:

- success
- HTTP error
- malformed response
- timeout/network error
- missing API key
- safe error handling

Security

Gunakan fake secrets.

Pastikan secret tidak muncul di:

- prompt;
- logs;
- exceptions;
- error messages;
- serialized metadata.

Side Effects

Provider tidak boleh:

- modify files;
- modify Git;
- commit;
- create HANDOFF.md;
- push.

---

9. CLI Integration

Integrasikan:

handoff --provider claude

dan config default provider.

Jika menambahkan diagnostic/config validation:

claude: configured

atau:

claude: missing API key

Jangan pernah mencetak API key.

---

10. Dry Run

Pertahankan:

handoff --dry-run

Dry run:

- boleh build context;
- boleh build prompt;
- TIDAK boleh menulis HANDOFF.md;
- TIDAK boleh commit;
- TIDAK boleh push.

Jika dry-run melakukan API request, behavior harus eksplisit dan dites.

Jangan membuat behavior yang diam-diam mengirim data ke provider jika user hanya meminta dry-run.

Preferensi untuk Phase 5A:

dry-run default tidak melakukan external AI request.

Jika perlu menguji provider, gunakan mode/provider test yang eksplisit.

---

Scope Boundary

Implement ONLY Phase 5A:

- ProviderAdapter
- provider registry/factory
- ClaudeProvider
- PromptBuilder
- configuration integration
- CLI provider selection
- tests

Jangan implement:

- HANDOFF.md generator
- file writing
- commit
- staging
- push
- automatic fallback
- OpenAI provider
- Qwen provider
- DeepSeek provider
- CHANGELOG.

Do not commit.
Do not push.
Do not modify target Lokalink project.

---

Regression

Run the complete test suite:

Phase 1–4 + Phase 5A.

Target:

all tests PASS.

After completion STOP.

Report:

1. files created/modified
2. provider architecture
3. registry/factory
4. Claude implementation
5. config schema
6. API-key handling
7. network boundary
8. PromptBuilder
9. dry-run behavior
10. tests
11. regression
12. security audit
13. known limitations

Wait for approval before Phase 5B.0

