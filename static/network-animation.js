/**
 * Network Animation — Decentralized Inference Network visualization
 *
 * Draws interconnected nodes with data packets pulsing between them,
 * illustrating LlamaNet's distributed inference architecture.
 *
 * - Gentle node drift with edge bouncing
 * - Data packets traveling between nearby nodes
 * - Pulse glow on packet arrival
 * - Responsive (fewer nodes on mobile)
 * - Accessible (respects prefers-reduced-motion)
 * - Performance (pauses when hero section not visible)
 */
const _NA = {
    DIST: 200,          // max connection distance (px)
    SPEED: 0.08,        // node drift speed (px/ms)
    PKT_SPEED: 0.0008,  // packet progress per ms (~1.25 s travel)
    SPAWN_LO: 700,      // min ms between packet spawns
    SPAWN_HI: 1500,     // max ms between packet spawns
    MAX_PKT: 8,         // max concurrent packets
    GLOW_DECAY: 0.002,  // pulse brightness loss per ms
};

class NetworkAnimation {

    constructor(canvas) {
        if (!canvas) return;
        this.canvas = canvas;
        this.ctx     = canvas.getContext('2d');
        this.nodes   = [];
        this.packets = [];
        this.running = true;
        this.visible = true;
        this.lastTime = 0;
        this.spawnTimer = 0;
        this.nextSpawn  = 0;
        this.dpr = Math.min(window.devicePixelRatio || 1, 2);
        this.w = 0;
        this.h = 0;
        this._loopActive = false;

        this.reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        this._resize();
        this._createNodes();
        this._scheduleSpawn();
        this._bindEvents();

        if (this.reducedMotion) {
            this._drawStatic();
        } else {
            this.lastTime = performance.now();
            this._loopActive = true;
            requestAnimationFrame(t => this._tick(t));
        }
    }

    /* ── helpers ──────────────────────────────────────────── */

    _nodeCount() {
        if (window.innerWidth < 576) return 8;
        if (window.innerWidth < 768) return 10;
        return 14;
    }

    _scheduleSpawn() {
        this.nextSpawn  = _NA.SPAWN_LO + Math.random() * (_NA.SPAWN_HI - _NA.SPAWN_LO);
        this.spawnTimer = 0;
    }

    /* ── setup ────────────────────────────────────────────── */

    _resize() {
        const parent = this.canvas.parentElement;
        if (!parent) return;
        const rect = parent.getBoundingClientRect();
        this.w = rect.width;
        this.h = rect.height;
        this.canvas.width  = this.w * this.dpr;
        this.canvas.height = this.h * this.dpr;
        this.canvas.style.width  = this.w + 'px';
        this.canvas.style.height = this.h + 'px';
        this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
        for (const n of this.nodes) {
            n.x = Math.min(n.x, this.w);
            n.y = Math.min(n.y, this.h);
        }
    }

    _createNodes() {
        this.nodes = [];
        const count = this._nodeCount();
        const w = this.w || window.innerWidth;
        const h = this.h || 300;
        for (let i = 0; i < count; i++) {
            this.nodes.push({
                x: Math.random() * w,
                y: Math.random() * h,
                vx: (Math.random() - 0.5) * _NA.SPEED * 2,
                vy: (Math.random() - 0.5) * _NA.SPEED * 2,
                r: 2 + Math.random() * 2.5,
                glow: 0,
            });
        }
    }

    _bindEvents() {
        let rt;
        window.addEventListener('resize', () => {
            clearTimeout(rt);
            rt = setTimeout(() => this._resize(), 150);
        });

        document.addEventListener('visibilitychange', () => {
            this.running = !document.hidden;
            if (this.running) this.lastTime = performance.now();
        });

        if ('IntersectionObserver' in window) {
            new IntersectionObserver(entries => {
                this.visible = entries[0].isIntersecting;
                if (this.visible) this.lastTime = performance.now();
            }, { threshold: 0.1 }).observe(this.canvas.parentElement);
        }

        window.matchMedia('(prefers-reduced-motion: reduce)')
            .addEventListener('change', e => {
                this.reducedMotion = e.matches;
                if (!this.reducedMotion && !this._loopActive) {
                    this.lastTime = performance.now();
                    this._loopActive = true;
                    requestAnimationFrame(t => this._tick(t));
                } else if (this.reducedMotion) {
                    this._drawStatic();
                }
            });
    }

    /* ── main loop ────────────────────────────────────────── */

    _tick(now) {
        requestAnimationFrame(t => this._tick(t));
        if (!this.running || !this.visible || this.reducedMotion) return;
        if (!this.w || !this.h) return;

        const dt = Math.min(now - this.lastTime, 50);
        this.lastTime = now;
        this._update(dt);
        this._draw();
    }

    /* ── simulation ───────────────────────────────────────── */

    _update(dt) {
        // drift nodes
        for (const n of this.nodes) {
            n.x += n.vx * dt;
            n.y += n.vy * dt;
            if (n.x < 0)     { n.x = 0;     n.vx =  Math.abs(n.vx); }
            if (n.x > this.w) { n.x = this.w; n.vx = -Math.abs(n.vx); }
            if (n.y < 0)     { n.y = 0;     n.vy =  Math.abs(n.vy); }
            if (n.y > this.h) { n.y = this.h; n.vy = -Math.abs(n.vy); }
            if (n.glow > 0) n.glow = Math.max(0, n.glow - _NA.GLOW_DECAY * dt);
        }

        // spawn packets
        this.spawnTimer += dt;
        if (this.spawnTimer >= this.nextSpawn &&
            this.packets.length < _NA.MAX_PKT &&
            this.nodes.length >= 2) {
            this._scheduleSpawn();
            this._spawnPacket();
        }

        // advance packets
        const arrived = [];
        this.packets = this.packets.filter(p => {
            p.t += _NA.PKT_SPEED * dt;
            if (p.t >= 1) { arrived.push(p.to); return false; }
            return true;
        });
        for (const idx of arrived) this.nodes[idx].glow = 1;
    }

    _spawnPacket() {
        const from = Math.floor(Math.random() * this.nodes.length);
        let best = -1, bestD = Infinity;
        for (let i = 0; i < this.nodes.length; i++) {
            if (i === from) continue;
            const dx = this.nodes[from].x - this.nodes[i].x;
            const dy = this.nodes[from].y - this.nodes[i].y;
            const d  = Math.sqrt(dx * dx + dy * dy);
            if (d < _NA.DIST && d < bestD) { bestD = d; best = i; }
        }
        if (best === -1) {
            best = Math.floor(Math.random() * this.nodes.length);
            while (best === from && this.nodes.length > 1)
                best = Math.floor(Math.random() * this.nodes.length);
        }
        this.packets.push({ from, to: best, t: 0 });
    }

    /* ── rendering ────────────────────────────────────────── */

    _draw() {
        const { ctx, nodes, w, h } = this;
        const dist = _NA.DIST;
        ctx.clearRect(0, 0, w, h);

        // ── connections ──
        ctx.lineWidth = 1;
        for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < nodes.length; j++) {
                const a = nodes[i], b = nodes[j];
                const dx = a.x - b.x, dy = a.y - b.y;
                const d = Math.sqrt(dx * dx + dy * dy);
                if (d < dist) {
                    ctx.strokeStyle = `rgba(255,255,255,${((1 - d / dist) * 0.12).toFixed(3)})`;
                    ctx.beginPath();
                    ctx.moveTo(a.x, a.y);
                    ctx.lineTo(b.x, b.y);
                    ctx.stroke();
                }
            }
        }

        // ── packets ──
        for (const p of this.packets) {
            const a = nodes[p.from], b = nodes[p.to];
            const x = a.x + (b.x - a.x) * p.t;
            const y = a.y + (b.y - a.y) * p.t;

            const grad = ctx.createRadialGradient(x, y, 0, x, y, 10);
            grad.addColorStop(0, 'rgba(147,197,253,0.7)');
            grad.addColorStop(1, 'rgba(147,197,253,0)');
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.arc(x, y, 10, 0, Math.PI * 2);
            ctx.fill();

            ctx.fillStyle = 'rgba(255,255,255,0.95)';
            ctx.beginPath();
            ctx.arc(x, y, 2, 0, Math.PI * 2);
            ctx.fill();
        }

        // ── nodes ──
        for (const n of nodes) {
            if (n.glow > 0.01) {
                const gr = n.r + n.glow * 10;
                const gg = ctx.createRadialGradient(n.x, n.y, n.r, n.x, n.y, gr + 6);
                gg.addColorStop(0, `rgba(147,197,253,${(n.glow * 0.35).toFixed(3)})`);
                gg.addColorStop(1, 'rgba(147,197,253,0)');
                ctx.fillStyle = gg;
                ctx.beginPath();
                ctx.arc(n.x, n.y, gr + 6, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.fillStyle = `rgba(255,255,255,${(0.4 + n.glow * 0.5).toFixed(3)})`;
            ctx.beginPath();
            ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    _drawStatic() {
        const { ctx, nodes, w, h } = this;
        const dist = _NA.DIST;
        ctx.clearRect(0, 0, w, h);

        ctx.lineWidth = 1;
        for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < nodes.length; j++) {
                const a = nodes[i], b = nodes[j];
                const dx = a.x - b.x, dy = a.y - b.y;
                const d = Math.sqrt(dx * dx + dy * dy);
                if (d < dist) {
                    ctx.strokeStyle = `rgba(255,255,255,${((1 - d / dist) * 0.08).toFixed(3)})`;
                    ctx.beginPath();
                    ctx.moveTo(a.x, a.y);
                    ctx.lineTo(b.x, b.y);
                    ctx.stroke();
                }
            }
        }
        for (const n of nodes) {
            ctx.fillStyle = 'rgba(255,255,255,0.35)';
            ctx.beginPath();
            ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
            ctx.fill();
        }
    }
}

/* ── auto-init ────────────────────────────────────────────── */
(function initNetworkAnimation() {
    const canvas = document.getElementById('network-canvas');
    if (canvas) new NetworkAnimation(canvas);
})();
