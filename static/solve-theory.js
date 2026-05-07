// Solve Theory widget. Two modes:
//   Mode A ("create"): paint regions, place stars — first user defines the
//                      day's canonical board. Pick order falls back to the
//                      order stars were placed in.
//   Mode B ("pick"):   board already exists; user taps stars in pick order.
// The widget serializes to three hidden inputs on form submit.

(function () {
  const root = document.getElementById('solve-theory');
  if (!root) return;
  const toggle = document.getElementById('solve-theory-toggle');
  const panel = document.getElementById('solve-theory-panel');
  if (!toggle || !panel) return;

  const sizeChoices = JSON.parse(root.dataset.sizeChoices || '[7,8,9,10]');
  const existing = root.dataset.boardPayload && root.dataset.boardPayload !== 'null'
    ? JSON.parse(root.dataset.boardPayload) : null;
  const canEditBoard = root.dataset.canEditBoard === '1';
  const editingExisting = existing !== null && !canEditBoard;

  // State.
  let size = existing ? existing.size : (sizeChoices[sizeChoices.length - 1] || 10);
  let regions = existing ? cloneGrid(existing.regions) : freshGrid(size, 0);
  let stars = existing ? existing.stars.map((s) => [s[0], s[1]]) : [];
  let pickOrder = []; // list of "r,c" strings
  let tool = existing && !canEditBoard ? 'pick' : 'paint:0';

  const grid = panel.querySelector('[data-grid]');
  const tools = panel.querySelector('[data-tools]');
  const hint = panel.querySelector('[data-hint]');
  const sizeGroup = panel.querySelector('[data-size-group]');
  const regionsField = panel.querySelector('[data-regions]');
  const starsField = panel.querySelector('[data-stars]');
  const pickField = panel.querySelector('[data-pick]');
  const form = document.getElementById('submit-form');

  toggle.addEventListener('change', () => {
    panel.hidden = !toggle.checked;
    if (toggle.checked) render();
  });

  // Lock size if the existing board is canonical (other users have logged
  // theories, so we can't edit it).
  if (existing && !canEditBoard) {
    sizeGroup.querySelectorAll('input[name=size]').forEach((r) => {
      r.disabled = true;
      r.checked = (parseInt(r.value, 10) === size);
    });
  } else {
    sizeGroup.addEventListener('change', (e) => {
      const newSize = parseInt(e.target.value, 10);
      if (!sizeChoices.includes(newSize)) return;
      // Re-init grid; preserve nothing because the shape changed.
      size = newSize;
      regions = freshGrid(size, 0);
      stars = [];
      pickOrder = [];
      // If we were on a region-tool that no longer exists, fall back.
      if (tool.startsWith('paint:')) {
        const idx = parseInt(tool.split(':')[1], 10);
        if (idx >= size) tool = 'paint:0';
      }
      render();
    });
  }

  function render() {
    renderTools();
    renderGrid();
    updateHint();
  }

  function renderTools() {
    tools.innerHTML = '';
    const isPickMode = existing && !canEditBoard;
    if (!isPickMode) {
      // N region paints.
      for (let i = 0; i < size; i++) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'theory-tool';
        btn.dataset.kind = `paint:${i}`;
        btn.innerHTML = `<span class="swatch theory-region-${i}"></span>R${i + 1}`;
        if (tool === `paint:${i}`) btn.dataset.active = '1';
        btn.addEventListener('click', () => { tool = `paint:${i}`; render(); });
        tools.appendChild(btn);
      }
      const star = document.createElement('button');
      star.type = 'button';
      star.className = 'theory-tool';
      star.dataset.kind = 'star';
      star.textContent = '★ Star';
      if (tool === 'star') star.dataset.active = '1';
      star.addEventListener('click', () => { tool = 'star'; render(); });
      tools.appendChild(star);
    }

    // Pick-order tool always available once stars exist.
    if (stars.length > 0) {
      const pick = document.createElement('button');
      pick.type = 'button';
      pick.className = 'theory-tool';
      pick.dataset.kind = 'pick';
      pick.textContent = '1·2·3 Pick order';
      if (tool === 'pick') pick.dataset.active = '1';
      pick.addEventListener('click', () => { tool = 'pick'; render(); });
      tools.appendChild(pick);
    }
  }

  function renderGrid() {
    grid.innerHTML = '';
    grid.style.gridTemplateColumns = `repeat(${size}, var(--cell))`;
    grid.dataset.mode = (existing && !canEditBoard) ? 'pick' : 'edit';
    const starSet = new Set(stars.map((s) => s.join(',')));
    for (let r = 0; r < size; r++) {
      for (let c = 0; c < size; c++) {
        const cell = document.createElement('div');
        cell.className = `theory-cell theory-region-${regions[r][c]}`;
        cell.dataset.r = r;
        cell.dataset.c = c;
        const key = `${r},${c}`;
        if (starSet.has(key)) cell.classList.add('has-star');
        const pickIdx = pickOrder.indexOf(key);
        if (pickIdx >= 0) {
          cell.classList.add('pick-marked');
          const num = document.createElement('span');
          num.className = 'pick-num';
          num.textContent = pickIdx + 1;
          cell.appendChild(num);
        }
        cell.addEventListener('click', onCell);
        grid.appendChild(cell);
      }
    }
  }

  function onCell(e) {
    const r = parseInt(e.currentTarget.dataset.r, 10);
    const c = parseInt(e.currentTarget.dataset.c, 10);
    const key = `${r},${c}`;
    const starSet = new Set(stars.map((s) => s.join(',')));

    if (tool.startsWith('paint:')) {
      const id = parseInt(tool.split(':')[1], 10);
      regions[r][c] = id;
    } else if (tool === 'star') {
      if (starSet.has(key)) {
        stars = stars.filter((s) => s[0] !== r || s[1] !== c);
        pickOrder = pickOrder.filter((k) => k !== key);
      } else {
        stars.push([r, c]);
      }
    } else if (tool === 'pick') {
      if (!starSet.has(key)) return;
      const idx = pickOrder.indexOf(key);
      if (idx >= 0) {
        pickOrder.splice(idx, 1);
      } else {
        pickOrder.push(key);
      }
    }
    render();
  }

  function updateHint() {
    if (existing && !canEditBoard) {
      hint.textContent =
        `Tap stars in the order you spotted them. ${pickOrder.length}/${stars.length} marked. ` +
        'Partial sequences are fine.';
      return;
    }
    if (tool.startsWith('paint:')) {
      hint.textContent = `Painting region ${parseInt(tool.split(':')[1], 10) + 1}. Tap cells to assign them.`;
    } else if (tool === 'star') {
      hint.textContent = `Place ${2 * size} stars total. Tap to toggle. Currently placed: ${stars.length}.`;
    } else if (tool === 'pick') {
      hint.textContent = `Tap stars in pick order. ${pickOrder.length}/${stars.length} marked.`;
    } else {
      hint.textContent = '';
    }
  }

  // Serialize state into hidden inputs at submit time.
  if (form) {
    form.addEventListener('submit', () => {
      if (!toggle.checked) return;
      regionsField.value = JSON.stringify(regions);
      // Keep stars sorted so server's set-compare works on either side.
      const sortedStars = stars.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      starsField.value = JSON.stringify(sortedStars);
      // If the user didn't bother marking pick-order in Mode A, seed it with
      // the placement order so the server has *something* to record.
      const fallback = !existing && pickOrder.length === 0
        ? stars.map((s) => `${s[0]},${s[1]}`)
        : pickOrder;
      pickField.value = JSON.stringify(fallback.map((k) => k.split(',').map((n) => parseInt(n, 10))));
    });
  }

  function freshGrid(n, fill) {
    return Array.from({ length: n }, () => Array(n).fill(fill));
  }
  function cloneGrid(g) {
    return g.map((row) => row.slice());
  }

  // First paint when the page loads in case the panel was already open.
  if (toggle.checked) panel.hidden = false;
  if (!panel.hidden) render();

  // If editing an existing board, render eagerly so the user sees it as
  // soon as they tick the checkbox.
  if (existing) render();
})();
