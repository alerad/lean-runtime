(function () {
  "use strict";

  var modes = {
    automatic: {
      command: "lean-runtime check Primes.lean",
      context: "standalone file",
      record: "automatic discovery",
      lines: [
        ["dim", "Reading declared imports", "source"],
        ["active", "Discovering an exact environment", "context"],
        ["dim", "Trying mathlib-v4.33.0 · leanprover/lean4:v4.33.0", "environment"],
        ["dim", "Running Lean", "lean"],
        ["ok", "✓ Primes.lean accepted in 4.45s", "record"]
      ]
    },
    pinned: {
      command: "lean-runtime check Primes.lean --using mathlib@v4.33.0",
      context: "explicit package release",
      record: "command line",
      lines: [
        ["dim", "Using explicit context mathlib@v4.33.0", "context"],
        ["dim", "Resolved mathlib-v4.33.0 · leanprover/lean4:v4.33.0", "environment"],
        ["dim", "Running Lean", "lean"],
        ["ok", "✓ Primes.lean accepted", "record"]
      ]
    },
    project: {
      command: "lean-runtime check",
      context: "pinned Lake project",
      record: "nearest Lake project",
      lines: [
        ["dim", "Using lean-toolchain and lake-manifest.json", "context"],
        ["dim", "Preparing the pinned project environment", "environment"],
        ["dim", "Running Lake", "lean"],
        ["ok", "✓ project accepted", "record"]
      ]
    }
  };

  var timers = [];
  var runNumber = 0;

  function clearTimers() {
    timers.forEach(function (timer) { window.clearTimeout(timer); });
    timers = [];
  }

  function addLine(terminal, kind, value) {
    var line = document.createElement("span");
    line.className = "lr-term-line lr-term-" + kind;
    line.textContent = value;
    terminal.appendChild(line);
  }

  function runDemo(demo, modeName, immediate) {
    clearTimers();
    runNumber += 1;
    var thisRun = runNumber;
    var mode = modes[modeName];
    var terminal = demo.querySelector("[data-terminal]");
    var state = demo.querySelector("[data-run-state]");
    var inspect = demo.querySelector("[data-inspect]");
    var record = demo.querySelector("[data-run-record]");
    var sourceContext = demo.querySelector("[data-source-context]");
    var recordContext = demo.querySelector("[data-record-context]");
    var mark = demo.querySelector("mark");
    terminal.innerHTML = "";
    record.hidden = true;
    inspect.setAttribute("aria-expanded", "false");
    inspect.disabled = true;
    state.textContent = "running";
    sourceContext.textContent = mode.context;
    recordContext.textContent = mode.record;
    mark.classList.remove("is-active");

    var delay = immediate ? 0 : 360;
    addLine(terminal, "command", "$ " + mode.command);

    mode.lines.forEach(function (entry, index) {
      timers.push(window.setTimeout(function () {
        if (runNumber !== thisRun) return;
        addLine(terminal, entry[0], entry[1]);
        terminal.scrollTop = terminal.scrollHeight;
        mark.classList.toggle("is-active", entry[2] === "source");

        if (index === mode.lines.length - 1) {
          state.textContent = "accepted";
          inspect.disabled = false;
        }
      }, immediate ? 0 : delay + index * 520));
    });
  }

  function init() {
    var demo = document.querySelector("[data-run-demo]");
    if (!demo || demo.dataset.initialized === "true") return;
    demo.dataset.initialized = "true";
    var currentMode = "automatic";
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    demo.querySelectorAll("[data-demo-mode]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        currentMode = tab.dataset.demoMode;
        demo.querySelectorAll("[data-demo-mode]").forEach(function (candidate) {
          candidate.setAttribute("aria-selected", candidate === tab ? "true" : "false");
        });
        runDemo(demo, currentMode, reduceMotion);
      });
    });

    demo.querySelector("[data-replay]").addEventListener("click", function () {
      runDemo(demo, currentMode, reduceMotion);
    });

    demo.querySelector("[data-inspect]").addEventListener("click", function () {
      var record = demo.querySelector("[data-run-record]");
      var expanded = this.getAttribute("aria-expanded") === "true";
      this.setAttribute("aria-expanded", expanded ? "false" : "true");
      record.hidden = expanded;
    });

    var copy = demo.querySelector("[data-copy-install]");
    copy.addEventListener("click", function () {
      var command = "python -m pip install lean-runtime";
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(command).then(function () {
        copy.textContent = "Copied";
        window.setTimeout(function () { copy.textContent = "Copy"; }, 1400);
      });
    });

    if (reduceMotion || !("IntersectionObserver" in window)) {
      runDemo(demo, currentMode, true);
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      if (entries.some(function (entry) { return entry.isIntersecting; })) {
        observer.disconnect();
        runDemo(demo, currentMode, false);
      }
    }, { threshold: .25 });
    observer.observe(demo);
  }

  if (typeof document$ !== "undefined") document$.subscribe(init);
  else document.addEventListener("DOMContentLoaded", init);
})();
