---
hide:
  - navigation
  - toc
  - footer
---

<nav class="lr-topbar" aria-label="Main navigation">
  <div class="lr-topbar-inner">
    <a class="lr-wordmark" href="./">lean<span>-</span>runtime</a>
    <span class="lr-version-chip">Lean 4 · Python 3.10+</span>
    <div class="lr-topnav">
      <a href="tutorial/">Start</a>
      <a href="workflows/check-files/">Workflows</a>
      <a href="concepts/context-selection/">Concepts</a>
      <a href="reference/commands/">Reference</a>
      <a href="trust/">Trust</a>
      <a href="https://github.com/alerad/lean-runtime">GitHub</a>
    </div>
  </div>
</nav>

<section class="lr-hero" data-run-demo>
  <div class="lr-hero-copy">
    <p class="lr-eyebrow"><span aria-hidden="true">⊢</span> Compiler-backed Lean execution</p>
    <h1>Check a Lean file in the environment it needs.</h1>
    <p class="lr-lead">Lean Runtime resolves an exact toolchain and dependency context, runs Lean, and records what actually ran.</p>
    <div class="lr-actions">
      <a class="md-button md-button--primary" href="tutorial/">Run your first check</a>
      <a class="md-button" href="concepts/context-selection/">How context is selected</a>
    </div>
    <div class="lr-install" aria-label="Install command">
      <code>python -m pip install lean-runtime</code>
      <button type="button" data-copy-install aria-label="Copy install command">Copy</button>
    </div>
    <p class="lr-platform-note">Python 3.10 or newer and Git are required. Automatic Elan bootstrap is available on macOS and Linux.</p>
  </div>

  <div class="lr-instrument" aria-label="Interactive Lean Runtime example">
    <div class="lr-mode-tabs" role="tablist" aria-label="Execution context">
      <button type="button" role="tab" aria-selected="true" data-demo-mode="automatic">Automatic</button>
      <button type="button" role="tab" aria-selected="false" data-demo-mode="pinned">Pinned</button>
      <button type="button" role="tab" aria-selected="false" data-demo-mode="project">Lake project</button>
    </div>

    <div class="lr-source-pane">
      <div class="lr-pane-bar">
        <span class="lr-pane-dot" aria-hidden="true"></span>
        <span data-source-name>Primes.lean</span>
        <span class="lr-pane-context" data-source-context>standalone file</span>
      </div>
      <pre data-source><code><span class="lr-kw">import</span> <mark>Mathlib.Data.Nat.Prime.Infinite</mark>

<span class="lr-kw">example</span> : ∀ n : ℕ, ∃ p, n ≤ p ∧ p.Prime :=
  Nat.exists_infinite_primes</code></pre>
    </div>

    <div class="lr-terminal-pane">
      <div class="lr-pane-bar">
        <span class="lr-pane-dot" aria-hidden="true"></span>
        terminal
        <span class="lr-pane-context" data-run-state>ready</span>
      </div>
      <div class="lr-terminal" data-terminal aria-live="polite"></div>
    </div>

    <div class="lr-demo-controls">
      <button type="button" class="lr-replay" data-replay><span aria-hidden="true">↻</span> Replay</button>
      <button type="button" class="lr-inspect-button" data-inspect aria-expanded="false">Inspect this run</button>
    </div>

    <div class="lr-run-record" data-run-record hidden>
      <dl>
        <div><dt>Context source</dt><dd data-record-context>automatic discovery</dd></div>
        <div><dt>Toolchain</dt><dd><code>leanprover/lean4:v4.33.0</code></dd></div>
        <div><dt>Environment</dt><dd><code>mathlib-v4.33.0</code></dd></div>
        <div><dt>Verdict</dt><dd class="lr-accepted">accepted</dd></div>
      </dl>
      <p>The environment is proposed from source evidence. The verdict is produced by Lean.</p>
    </div>
  </div>
</section>



<section class="lr-workflows" aria-labelledby="workflows-title">
  <div class="lr-section-heading">
    <p class="lr-kicker">Workflows</p>
    <h2 id="workflows-title">Use the entry point that matches your work.</h2>
  </div>
  <div class="lr-card-grid">
    <article>
      <p class="lr-card-label">Standalone file</p>
      <h3>Bring a Lean source file</h3>
      <pre><code>lean-runtime check Main.lean</code></pre>
      <p>Lean Runtime discovers a plausible exact environment and accepts it only when Lean accepts the file.</p>
      <a href="workflows/check-files/">Check Lean files</a>
    </article>
    <article>
      <p class="lr-card-label">Lake project</p>
      <h3>Keep the project authoritative</h3>
      <pre><code>lean-runtime adopt . --yes
lean-runtime build</code></pre>
      <p>The project keeps its pinned toolchain and manifest. Dependency storage and compatible artifacts can be reused.</p>
      <a href="workflows/lake-projects/">Work with Lake projects</a>
    </article>
    <article>
      <p class="lr-card-label">Exact and offline</p>
      <h3>Record the environment</h3>
      <pre><code>lean-runtime check Main.lean \
  --write-lock environment.lock.json</code></pre>
      <p>Reuse the resulting lock explicitly, including with network access disabled.</p>
      <a href="workflows/check-files/#record-and-reuse-an-exact-lock">Use an exact lock</a>
    </article>
  </div>
</section>

