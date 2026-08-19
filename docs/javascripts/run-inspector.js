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
        ["ok", "✓ Primes.lean accepted in 1.39s", "record"]
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

  var details = {
    source: ["Source", "Lean Runtime reads the imports declared by the file. It does not execute the source during this step."],
    context: ["Context", "Explicit input takes precedence. Otherwise the nearest pinned project or automatic discovery supplies the context."],
    environment: ["Environment", "The selected toolchain and exact package revisions are acquired or reused, then checked against their recorded identities."],
    lean: ["Lean", "Lean or Lake runs inside the selected context. Compiler diagnostics remain attached to the source file."],
    record: ["Result", "The result distinguishes acceptance, compiler rejection, and infrastructure failure while recording execution provenance."]
  };

  var timers = [];
  var runNumber = 0;

  function clearTimers() {
    timers.forEach(function (timer) { window.clearTimeout(timer); });
    timers = [];
  }

  function setProcess(root, active, completeThrough) {
    root.querySelectorAll("[data-process-step]").forEach(function (button) {
      var names = ["source", "context", "environment", "lean", "record"];
      var index = names.indexOf(button.dataset.processStep);
      button.classList.toggle("is-running", button.dataset.processStep === active);
      button.classList.toggle("is-complete", index <= completeThrough);
    });
  }

  function selectProcess(root, key) {
    root.querySelectorAll("[data-process-step]").forEach(function (button) {
      var selected = button.dataset.processStep === key;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    showDetail(root, key);
  }

  function showDetail(root, key) {
    var detail = root.querySelector("[data-process-detail]");
    if (!detail || !details[key]) return;
    detail.innerHTML = "<strong>" + details[key][0] + "</strong><p>" + details[key][1] + "</p>";
  }

  function addLine(terminal, kind, value) {
    var line = document.createElement("span");
    line.className = "lr-term-line lr-term-" + kind;
    line.textContent = value;
    terminal.appendChild(line);
  }

  function runDemo(demo, page, modeName, immediate) {
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
    setProcess(page, "source", -1);
    if (!page.querySelector("[data-process-step].is-selected")) selectProcess(page, "source");

    var delay = immediate ? 0 : 360;
    addLine(terminal, "command", "$ " + mode.command);

    mode.lines.forEach(function (entry, index) {
      timers.push(window.setTimeout(function () {
        if (runNumber !== thisRun) return;
        addLine(terminal, entry[0], entry[1]);
        terminal.scrollTop = terminal.scrollHeight;
        var stepIndex = ["source", "context", "environment", "lean", "record"].indexOf(entry[2]);
        setProcess(page, entry[2], stepIndex - 1);
        mark.classList.toggle("is-active", entry[2] === "source");

        if (index === mode.lines.length - 1) {
          state.textContent = "accepted";
          inspect.disabled = false;
          setProcess(page, "record", 4);
        }
      }, immediate ? 0 : delay + index * 520));
    });
  }

  function init() {
    var demo = document.querySelector("[data-run-demo]");
    if (!demo || demo.dataset.initialized === "true") return;
    demo.dataset.initialized = "true";
    var page = demo.closest("main") || document;
    var currentMode = "automatic";
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    demo.querySelectorAll("[data-demo-mode]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        currentMode = tab.dataset.demoMode;
        demo.querySelectorAll("[data-demo-mode]").forEach(function (candidate) {
          candidate.setAttribute("aria-selected", candidate === tab ? "true" : "false");
        });
        runDemo(demo, page, currentMode, reduceMotion);
      });
    });

    demo.querySelector("[data-replay]").addEventListener("click", function () {
      runDemo(demo, page, currentMode, reduceMotion);
    });

    demo.querySelector("[data-inspect]").addEventListener("click", function () {
      var record = demo.querySelector("[data-run-record]");
      var expanded = this.getAttribute("aria-expanded") === "true";
      this.setAttribute("aria-expanded", expanded ? "false" : "true");
      record.hidden = expanded;
    });

    page.querySelectorAll("[data-process-step]").forEach(function (button) {
      button.addEventListener("click", function () { selectProcess(page, button.dataset.processStep); });
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
      runDemo(demo, page, currentMode, true);
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      if (entries.some(function (entry) { return entry.isIntersecting; })) {
        observer.disconnect();
        runDemo(demo, page, currentMode, false);
      }
    }, { threshold: .25 });
    observer.observe(demo);
  }

  if (typeof document$ !== "undefined") document$.subscribe(init);
  else document.addEventListener("DOMContentLoaded", init);
})();
