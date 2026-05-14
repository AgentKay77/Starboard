// Solve Theory widget. Two modes:
//   Mode A ("create"): paint regions, place stars — first user defines the
//                      day's canonical board. Pick order falls back to the
//                      order stars were placed in.
//   Mode B ("pick"):   board already exists; user taps stars in pick order.
// Painting supports drag (mouse + touch via pointer events) so a single
// swipe can fill an entire region without retapping every cell.
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

  // State.  Unpainted cells use -1 as a sentinel so a tap with any region
  // tool produces an obvious visible change. validate_board() on the server
  // rejects -1, so the user is forced to fill the grid before submitting.
  const UNPAINTED = -1;
  let size = existing ? existing.size : (sizeChoices[sizeChoices.length - 1] || 10);
  let regions = existing ? cloneGrid(existing.regions) : freshGrid(size, UNPAINTED);
  // `stars` is the live merged set the UI works with — existing canonical
  // stars PLUS anything the current user has added in this session.
  // `canonicalStars` is a frozen snapshot so we can disallow removing
  // stars somebody else placed.
  let stars = existing ? existing.stars.map((s) => [s[0], s[1]]) : [];
  const canonicalStars = existing
    ? new Set(existing.stars.map((s) => `${s[0]},${s[1]}`))
    : new Set();
  let pickOrder = []; // list of "r,c" strings
  // Per-user X marks — deductions ("can't be a star"). Stored on the
  // user's solve_theories row, not on the canonical board.
  let xs = new Set();
  let tool = existing && !canEditBoard ? 'pick' : 'paint:0';

  const grid = panel.querySelector('[data-grid]');
  const tools = panel.querySelector('[data-tools]');
  const hint = panel.querySelector('[data-hint]');
  const sizeGroup = panel.querySelector('[data-size-group]');
  const regionsField = panel.querySelector('[data-regions]');
  const starsField = panel.querySelector('[data-stars]');
  const pickField = panel.querySelector('[data-pick]');
  const xsField = panel.querySelector('[data-xs]');
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
      regions = freshGrid(size, UNPAINTED);
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

  // ---- Rendering ---------------------------------------------------------
  // We update a single cell in place rather than rebuilding the grid on
  // every tap. That's necessary for drag-paint to work — full re-renders
  // would orphan the captured pointer mid-drag.

  function applyCellClasses(div, r, c) {
    if (!div) return;
    const rid = regions[r][c];
    const regionClass = rid === UNPAINTED
      ? 'theory-region-unpainted'
      : `theory-region-${rid}`;
    const rightDiffers = c < size - 1 && regions[r][c + 1] !== rid;
    const belowDiffers = r < size - 1 && regions[r + 1][c] !== rid;
    const hasStar = stars.some((s) => s[0] === r && s[1] === c);
    const pickIdx = pickOrder.indexOf(`${r},${c}`);

    const classes = ['theory-cell', regionClass];
    if (rightDiffers) classes.push('boundary-right');
    if (belowDiffers) classes.push('boundary-bottom');
    if (c === size - 1) classes.push('frame-right');
    if (r === size - 1) classes.push('frame-bottom');
    if (hasStar) classes.push('has-star');
    if (xs.has(`${r},${c}`) && !hasStar) classes.push('has-x');
    if (pickIdx >= 0) classes.push('pick-marked');
    div.className = classes.join(' ');

    const oldNum = div.querySelector('.pick-num');
    if (oldNum) oldNum.remove();
    if (pickIdx >= 0) {
      const num = document.createElement('span');
      num.className = 'pick-num';
      num.textContent = pickIdx + 1;
      div.appendChild(num);
    }
  }

  function getCellEl(r, c) {
    return grid.querySelector(`[data-r="${r}"][data-c="${c}"]`);
  }

  // Refresh the painted cell plus its top/left neighbours, since their
  // right/bottom boundary classes flip when *this* cell's region changes.
  function refreshCellAndNeighbors(r, c) {
    applyCellClasses(getCellEl(r, c), r, c);
    if (r > 0) applyCellClasses(getCellEl(r - 1, c), r - 1, c);
    if (c > 0) applyCellClasses(getCellEl(r, c - 1), r, c - 1);
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
      for (let i = 0; i < size; i++) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'theory-tool';
        btn.dataset.kind = `paint:${i}`;
        const ct = countCellsInRegion(i);
        btn.innerHTML =
          `<span class="swatch theory-region-${i}"></span>` +
          `R${i + 1}` +
          (ct > 0
            ? ` <span class="ct" style="opacity: 0.7; font-size: 11px; margin-left: 2px;">${ct}</span>`
            : '');
        if (ct >= 2) btn.dataset.full = '1';
        if (tool === `paint:${i}`) btn.dataset.active = '1';
        btn.addEventListener('click', () => { tool = `paint:${i}`; render(); });
        tools.appendChild(btn);
      }
    }

    // ★ Star tool is available in BOTH modes. Mode A: place every star
    // yourself. Mode B: add stars the first author left out (canonical
    // ones are protected from removal).
    const star = document.createElement('button');
    star.type = 'button';
    star.className = 'theory-tool';
    star.dataset.kind = 'star';
    star.textContent = '★ Star';
    if (tool === 'star') star.dataset.active = '1';
    star.addEventListener('click', () => { tool = 'star'; render(); });
    tools.appendChild(star);

    // ✗ X tool — per-user "this cell can't be a star" deduction. Doesn't
    // touch the canonical board; saved to the user's solve_theories row.
    const xBtn = document.createElement('button');
    xBtn.type = 'button';
    xBtn.className = 'theory-tool';
    xBtn.dataset.kind = 'x';
    xBtn.textContent = '✗ X';
    if (tool === 'x') xBtn.dataset.active = '1';
    xBtn.addEventListener('click', () => { tool = 'x'; render(); });
    tools.appendChild(xBtn);

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
    for (let r = 0; r < size; r++) {
      for (let c = 0; c < size; c++) {
        const cell = document.createElement('div');
        cell.dataset.r = r;
        cell.dataset.c = c;
        applyCellClasses(cell, r, c);
        grid.appendChild(cell);
      }
    }
  }

  // ---- Interaction -------------------------------------------------------
  // Painting is drag-friendly via pointer events: pointerdown on a cell
  // starts the gesture, pointermove walks across additional cells, and we
  // lift on pointerup/cancel/leave. Star/pick tools remain click-only —
  // dragging through stars to toggle them is more confusing than helpful.

  let drag = null;  // { paintId: number, lastKey: string } | null

  function paintAt(r, c) {
    if (!drag) return;
    if (regions[r][c] === drag.paintId) return;
    regions[r][c] = drag.paintId;
    refreshCellAndNeighbors(r, c);
  }

  function cellFromPoint(x, y) {
    const el = document.elementFromPoint(x, y);
    if (!el) return null;
    const cell = el.closest('.theory-cell');
    if (!cell || !grid.contains(cell)) return null;
    return cell;
  }

  grid.addEventListener('pointerdown', (e) => {
    const cell = e.target.closest('.theory-cell');
    if (!cell) return;
    const r = parseInt(cell.dataset.r, 10);
    const c = parseInt(cell.dataset.c, 10);

    if (tool.startsWith('paint:')) {
      const id = parseInt(tool.split(':')[1], 10);
      drag = { paintId: id, lastKey: '' };
      // Capture so subsequent move events keep firing even if the finger
      // briefly leaves the cell during the gesture.
      try { grid.setPointerCapture(e.pointerId); } catch (_) { /* mouse-only on some browsers */ }
      paintAt(r, c);
      drag.lastKey = `${r},${c}`;
      e.preventDefault();
      return;
    }

    // Star + pick tools: per-tap toggles, no drag.
    handleTap(r, c);
  });

  grid.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const cell = cellFromPoint(e.clientX, e.clientY);
    if (!cell) return;
    const r = parseInt(cell.dataset.r, 10);
    const c = parseInt(cell.dataset.c, 10);
    const key = `${r},${c}`;
    if (key === drag.lastKey) return;
    paintAt(r, c);
    drag.lastKey = key;
  });

  function endDrag(e) {
    if (!drag) return;
    drag = null;
    try { if (e && e.pointerId != null) grid.releasePointerCapture(e.pointerId); } catch (_) { /* ignore */ }
    updateHint();
  }
  grid.addEventListener('pointerup', endDrag);
  grid.addEventListener('pointercancel', endDrag);
  grid.addEventListener('pointerleave', endDrag);

  function handleTap(r, c) {
    const key = `${r},${c}`;
    const starSet = new Set(stars.map((s) => s.join(',')));
    if (tool === 'star') {
      if (starSet.has(key)) {
        if (canonicalStars.has(key)) {
          // Don't let users remove stars somebody else placed —
          // protected as part of the canonical board.
          return;
        }
        stars = stars.filter((s) => s[0] !== r || s[1] !== c);
        pickOrder = pickOrder.filter((k) => k !== key);
      } else {
        // Placing a star clears any X on the same cell.
        xs.delete(key);
        stars.push([r, c]);
      }
      render();
    } else if (tool === 'x') {
      // X marks "can't be a star". Disallowed on a star (clearly *can*
      // be one — it IS one).
      if (starSet.has(key)) return;
      if (xs.has(key)) xs.delete(key);
      else xs.add(key);
      refreshCellAndNeighbors(r, c);
      updateHint();
    } else if (tool === 'pick') {
      if (!starSet.has(key)) return;
      const idx = pickOrder.indexOf(key);
      if (idx >= 0) pickOrder.splice(idx, 1);
      else pickOrder.push(key);
      // Pick numbering shifts on every other cell, refresh whole grid.
      renderGrid();
      updateHint();
    }
  }

  function updateHint() {
    if (existing && !canEditBoard) {
      if (tool === 'star') {
        hint.textContent =
          `Add stars the first player missed. Canonical stars (from the original ` +
          `board) are protected — you can only remove ones you placed yourself. ` +
          `Total on board: ${stars.length} / ${2 * size}.`;
      } else {
        hint.textContent =
          `Tap stars in the order you spotted them. ${pickOrder.length}/${stars.length} marked. ` +
          `Partial sequences are fine.`;
      }
      return;
    }
    const unpaintedCount = countUnpainted();
    if (tool.startsWith('paint:')) {
      const regionIdx = parseInt(tool.split(':')[1], 10);
      const ownCount = countCellsInRegion(regionIdx);
      hint.textContent =
        `Painting region ${regionIdx + 1} (${ownCount} cell${ownCount === 1 ? '' : 's'}). ` +
        `${unpaintedCount} cell(s) unpainted. Drag to fill — region size can vary.`;
    } else if (tool === 'star') {
      hint.textContent =
        `Place up to ${2 * size} stars (no two touching). Tap to toggle. ` +
        `Currently placed: ${stars.length}. Partial placements are fine — only mark stars you're confident about.`;
    } else if (tool === 'x') {
      hint.textContent =
        `X marks cells that can't be a star. Personal deduction notes — saved with your theory, not part of the canonical board. Currently: ${xs.size}.`;
    } else if (tool === 'pick') {
      hint.textContent = `Tap stars in pick order. ${pickOrder.length}/${stars.length} marked.`;
    } else {
      hint.textContent = '';
    }
  }

  function countUnpainted() {
    let n = 0;
    for (let r = 0; r < size; r++)
      for (let c = 0; c < size; c++)
        if (regions[r][c] === UNPAINTED) n++;
    return n;
  }
  function countCellsInRegion(rid) {
    let n = 0;
    for (let r = 0; r < size; r++)
      for (let c = 0; c < size; c++)
        if (regions[r][c] === rid) n++;
    return n;
  }

  // Serialize state into hidden inputs at submit time.
  if (form) {
    form.addEventListener('submit', (e) => {
      if (!toggle.checked) return;
      if (!existing) {
        const unpainted = countUnpainted();
        if (unpainted > 0) {
          e.preventDefault();
          alert(
            `Solve theory: ${unpainted} cell(s) still unpainted. ` +
            `Pick a region color and drag/tap to fill them, or untick "Solve Theory" to skip.`
          );
          return;
        }
        // Every region 0..size-1 must be used at least twice (a region
        // with one cell can't hold two non-touching stars). Cell counts
        // can otherwise vary — Stars puzzles ship with irregular regions.
        const tally = new Array(size).fill(0);
        for (let r = 0; r < size; r++)
          for (let c = 0; c < size; c++) tally[regions[r][c]]++;
        const missing = [];
        const tooSmall = [];
        for (let i = 0; i < size; i++) {
          if (tally[i] === 0) missing.push(`R${i + 1}`);
          else if (tally[i] < 2) tooSmall.push(`R${i + 1} has ${tally[i]} cell`);
        }
        if (missing.length) {
          e.preventDefault();
          alert(
            `Solve theory: every region must be painted. Missing: ${missing.join(', ')}. ` +
            `Pick that color and paint at least 2 cells with it.`
          );
          return;
        }
        if (tooSmall.length) {
          e.preventDefault();
          alert(
            `Solve theory: each region needs at least 2 cells (to fit two non-touching stars). ` +
            `Too small: ${tooSmall.join('; ')}.`
          );
          return;
        }
        if (stars.length > 2 * size) {
          e.preventDefault();
          alert(
            `Solve theory: too many stars (${stars.length}). The puzzle has ` +
            `at most ${2 * size}. Remove some with the ★ Star tool.`
          );
          return;
        }
        // Partial placements are fine — Hunter wants players to log what
        // they actually spotted, not be forced into a full 2N grid.
      }
      regionsField.value = JSON.stringify(regions);
      const sortedStars = stars.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      starsField.value = JSON.stringify(sortedStars);
      const fallback = !existing && pickOrder.length === 0
        ? stars.map((s) => `${s[0]},${s[1]}`)
        : pickOrder;
      pickField.value = JSON.stringify(fallback.map((k) => k.split(',').map((n) => parseInt(n, 10))));
      if (xsField) {
        xsField.value = JSON.stringify(
          Array.from(xs).map((k) => k.split(',').map((n) => parseInt(n, 10)))
        );
      }
    });
  }

  function freshGrid(n, fill) {
    return Array.from({ length: n }, () => Array(n).fill(fill));
  }
  function cloneGrid(g) {
    return g.map((row) => row.slice());
  }

  if (toggle.checked) panel.hidden = false;
  if (!panel.hidden) render();
  if (existing) render();
})();
