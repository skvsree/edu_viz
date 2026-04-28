
let phaserGame = null;


// ─── Phaser 3 Scene: Topic 1 Food Diversity ─────────────────────────────────
// Full redesign: dramatic food close-up overlays with cinematic transitions
class PhaserFoodDiversityScene extends Phaser.Scene {
  constructor() { super({ key: 'FoodDiversity' }); }

  create() {
    // NOTE: animationState may not be initialized yet when scene is created.
    // We read it dynamically so that hotspot flow works regardless of init order.
    // Lazy proxies — all `this.state` / `this.topic` calls resolve at access time.
    Object.defineProperty(this, 'state', { get: () => animationState, configurable: true });
    Object.defineProperty(this, 'topic', { get: () => topics[0], configurable: true });
    this.bubbleEl = document.getElementById('speech-bubble-text');
    this.detailPanel = document.getElementById('selection-detail-panel');
    this.instructionEl = document.getElementById('animation-instruction');

    this.width = this.scale.gameSize.width;
    this.height = this.scale.gameSize.height;
    this.groundY = this.height - 90;

    this.antX = this.width * 0.2;
    this.antTargetX = this.width * 0.2;
    this.antWalking = false;
    this.antSpeed = 3;

    this.closeupOpen = false;
    this.closeupBg = null;
    this.closeupStage = null;
    this.closeupBubble = null;
    this.closeupContinueBtn = null;
    this.closeupLabel = null;
    this.ingredientBadges = [];

    this.drawBackground();
    this.drawFoodItems();
    this.drawAnt();
    this.setupHotspots();
    this.updateBubbleUI();
    this.input.on('pointerdown', (ptr) => this.handleTap(ptr));
  }

  drawBackground() {
    const bg = this.add.graphics();
    // Deep night sky — layered gradient feel via color stops
    bg.fillStyle(0x0a1628, 1);
    bg.fillRect(0, 0, this.width, this.height);
    // Subtle sky gradient overlay (lighter band near horizon)
    bg.fillStyle(0x1a2d4a, 0.4);
    bg.fillRect(0, 0, this.width, this.groundY - 60);
    // Horizon glow
    bg.fillStyle(0x2a3d5a, 0.25);
    bg.fillRect(0, this.groundY - 80, this.width, 80);

    // Rich star field — varied sizes, colors, brightness
    const starColors = [0xffeedd, 0xddeeff, 0xffeecc, 0xffffff, 0xccddff];
    for (let i = 0; i < 90; i++) {
      const sx = Phaser.Math.Between(0, this.width);
      const sy = Phaser.Math.Between(0, this.groundY - 100);
      const r = Phaser.Math.FloatBetween(0.4, 2.0);
      const c = Phaser.Math.RND.pick(starColors);
      const a = Phaser.Math.FloatBetween(0.35, 0.9);
      bg.fillStyle(c, a);
      bg.fillCircle(sx, sy, r);
      // Occasional twinkle — larger star gets a subtle halo
      if (r > 1.5 && Phaser.Math.Between(0, 3) === 0) {
        bg.fillStyle(c, a * 0.3);
        bg.fillCircle(sx, sy, r * 2.5);
      }
    }

    // Ground — layered green with subtle gradient
    bg.fillStyle(0x2e7d32, 0.7);
    bg.fillRoundedRect(0, this.groundY, this.width, 90, { tl: 0, tr: 0, bl: 24, br: 24 });
    bg.fillStyle(0x388e3c, 0.5);
    bg.fillRoundedRect(0, this.groundY, this.width, 55, { tl: 0, tr: 0, bl: 16, br: 16 });
    // Ground highlight strip
    bg.fillStyle(0x4caf50, 0.4);
    bg.fillRect(0, this.groundY, this.width, 8);
  }

  drawFoodItems() {
    const g = this.add.graphics();
    const gw = this.width, gy = this.groundY;

    // ── PUNJAB MEAL (x=18%) ──────────────────────────────────────────────────
    const px1 = gw * 0.18;
    // Shadow under plate
    g.fillStyle(0x000000, 0.18);
    g.fillEllipse(px1, gy + 6, 140, 22);
    // Banana leaf — base
    g.fillStyle(0x1b5e20, 0.95);
    g.fillEllipse(px1, gy, 145, 36);
    g.fillStyle(0x2e7d32, 0.85);
    g.fillEllipse(px1, gy - 2, 130, 28);
    // Leaf vein lines
    g.lineStyle(1, 0x1b5e20, 0.4);
    g.beginPath();
    g.moveTo(px1 - 60, gy); g.lineTo(px1 + 60, gy);
    g.strokePath();
    // Plate base
    g.fillStyle(0xf5deb3, 0.95);
    g.fillEllipse(px1, gy - 4, 125, 26);
    g.fillStyle(0xe8c89a, 0.9);
    g.fillEllipse(px1, gy - 6, 110, 20);

    // Makki di Roti — 3 layered rotis with char marks and texture
    const rotiData = [
      { off: -30, rot: -0.18, c1: 0xf5c518, c2: 0xe8b440, charX: [-25, 5, -5] },
      { off: 0,   rot:  0.05, c1: 0xf0c040, c2: 0xd4a030, charX: [-15, 20, 8] },
      { off: 28,  rot:  0.22, c1: 0xe8b830, c2: 0xc49020, charX: [-20, 12, -8] },
    ];
    rotiData.forEach(({ off, rot, c1, c2, charX }) => {
      // roti body
      g.fillStyle(c1, 0.95);
      g.fillEllipse(px1 + off, gy - 12, 125, 26);
      // darker edge shadow
      g.fillStyle(c2, 0.5);
      g.fillEllipse(px1 + off + 2, gy - 10, 118, 22);
      // char spots — dark brown/black marks
      charX.forEach((cx, i) => {
        g.fillStyle(0x8B4513, 0.65 + i * 0.05);
        g.fillCircle(px1 + off + cx, gy - 14 + (i % 2) * 4, 5 + i * 1.5);
      });
      // slight highlight on top
      g.fillStyle(0xfff8b0, 0.25);
      g.fillEllipse(px1 + off - 10, gy - 16, 40, 8);
    });

    // Sarson da Saag — bowl of greens with layered texture
    g.fillStyle(0x1b5e20, 0.95);
    g.fillEllipse(px1 - 72, gy - 2, 100, 38);
    g.fillStyle(0x2e7d32, 0.9);
    g.fillEllipse(px1 - 72, gy - 5, 88, 30);
    g.fillStyle(0x388e3c, 0.85);
    g.fillEllipse(px1 - 72, gy - 8, 74, 24);
    g.fillStyle(0x43a047, 0.75);
    g.fillEllipse(px1 - 72, gy - 10, 58, 18);
    // Saag surface texture — darker swirl
    g.fillStyle(0x1b5e20, 0.4);
    g.fillEllipse(px1 - 78, gy - 9, 30, 12);
    g.fillEllipse(px1 - 65, gy - 7, 20, 8);
    // Ghee dollops — bright yellow with highlight
    g.fillStyle(0xffd700, 0.95);
    g.fillCircle(px1 - 88, gy - 14, 10);
    g.fillCircle(px1 - 64, gy - 16, 8);
    g.fillCircle(px1 - 76, gy - 18, 6);
    // Ghee highlights
    g.fillStyle(0xffff88, 0.6);
    g.fillCircle(px1 - 90, gy - 17, 3);
    g.fillCircle(px1 - 66, gy - 19, 2.5);

    // Onion rings — sliced rings visible
    g.fillStyle(0xf8f0e0, 0.9);
    g.fillEllipse(px1 + 100, gy - 8, 55, 20);
    g.fillStyle(0xd4a0a0, 0.6);
    g.fillEllipse(px1 + 100, gy - 8, 40, 14);
    g.lineStyle(2, 0xe8c8c8, 0.5);
    g.strokeEllipse(px1 + 100, gy - 8, 30, 10);
    g.lineStyle(1, 0xe8c8c8, 0.4);
    g.strokeEllipse(px1 + 100, gy - 8, 20, 6);

    // Butter pat with wrapper
    g.fillStyle(0xfff176, 0.95);
    g.fillRoundedRect(px1 + 88, gy - 16, 30, 20, 4);
    g.fillStyle(0xf0e060, 0.8);
    g.fillRect(px1 + 88, gy - 16, 30, 8); // butter wrapper top

    // ── SOUTH INDIAN MEAL (x=50%) ─────────────────────────────────────────────
    const px2 = gw * 0.5;
    // Shadow
    g.fillStyle(0x000000, 0.18);
    g.fillEllipse(px2, gy + 6, 155, 24);
    // Banana leaf
    g.fillStyle(0x1b5e20, 0.95);
    g.fillEllipse(px2, gy, 155, 38);
    g.fillStyle(0x2e7d32, 0.85);
    g.fillEllipse(px2, gy - 2, 138, 30);
    g.lineStyle(1, 0x1b5e20, 0.4);
    g.beginPath(); g.moveTo(px2 - 65, gy); g.lineTo(px2 + 65, gy); g.strokePath();
    // Plate
    g.fillStyle(0xf5deb3, 0.95);
    g.fillEllipse(px2, gy - 4, 135, 28);
    g.fillStyle(0xe8c89a, 0.9);
    g.fillEllipse(px2, gy - 6, 120, 22);

    // Idli plate — 3 idlis with steam
    const idliX = [px2 - 72, px2, px2 + 72];
    const idliY = [0, -8, 0];
    idliX.forEach((ix, idx) => {
      // idli base shadow
      g.fillStyle(0xc8b89a, 0.8);
      g.fillEllipse(ix, gy - 2 + idliY[idx] / 4, 76, 26);
      // idli body
      g.fillStyle(0xf5f5dc, 0.95);
      g.fillEllipse(ix, gy - 8 + idliY[idx], 76, 26);
      // idli top dome
      g.fillStyle(0xfafaf0, 0.9);
      g.fillEllipse(ix, gy - 12 + idliY[idx], 68, 20);
      // idli highlight
      g.fillStyle(0xffffff, 0.35);
      g.fillEllipse(ix - 8, gy - 16 + idliY[idx], 24, 8);
      // Steam wisps (animated separately)
      g.fillStyle(0xffffff, 0.12);
      g.fillEllipse(ix - 10, gy - 35 + idliY[idx], 10, 16);
      g.fillEllipse(ix + 8, gy - 40 + idliY[idx], 8, 12);
    });

    // Sambhar bowl
    g.fillStyle(0xbf360c, 0.85);
    g.fillEllipse(px2 + 95, gy - 2, 95, 38);
    g.fillStyle(0xd84315, 0.8);
    g.fillEllipse(px2 + 95, gy - 5, 82, 30);
    g.fillStyle(0xe64a19, 0.7);
    g.fillEllipse(px2 + 95, gy - 8, 68, 24);
    // Sambhar surface shine
    g.fillStyle(0xff7043, 0.3);
    g.fillEllipse(px2 + 95, gy - 10, 50, 18);
    // Veggies in sambhar
    g.fillStyle(0x66bb6a, 0.85);
    g.fillEllipse(px2 + 82, gy - 10, 16, 10);
    g.fillEllipse(px2 + 100, gy - 12, 12, 8);
    g.fillStyle(0xffeb3b, 0.75);
    g.fillEllipse(px2 + 108, gy - 8, 10, 7);
    g.fillStyle(0xe65100, 0.75);
    g.fillEllipse(px2 + 92, gy - 13, 9, 6);

    // Coconut chutney
    g.fillStyle(0xf5f5dc, 0.9);
    g.fillEllipse(px2 - 105, gy - 2, 75, 28);
    g.fillStyle(0xfffde7, 0.8);
    g.fillEllipse(px2 - 105, gy - 5, 62, 22);
    // Green chutney swirl
    g.fillStyle(0x43a047, 0.7);
    g.fillEllipse(px2 - 112, gy - 7, 24, 13);
    g.fillStyle(0x388e3c, 0.5);
    g.fillEllipse(px2 - 108, gy - 8, 14, 8);
    // Chutney highlight
    g.fillStyle(0xffffff, 0.25);
    g.fillEllipse(px2 - 118, gy - 10, 10, 6);

    // Papad — crisp and golden
    g.fillStyle(0xf0d870, 0.85);
    g.fillEllipse(px2 + 18, gy + 6, 95, 18);
    g.fillStyle(0xe8c040, 0.65);
    g.fillEllipse(px2 + 18, gy + 6, 72, 12);
    g.lineStyle(1.5, 0xd4a030, 0.4);
    g.strokeEllipse(px2 + 18, gy + 6, 60, 8);

    // ── TRADITIONAL CHULHA (x=78%) ──────────────────────────────────────────
    const px3 = gw * 0.78;
    // Hearth shadow
    g.fillStyle(0x000000, 0.22);
    g.fillEllipse(px3, gy + 8, 100, 22);
    // Clay body — main cylinder
    g.fillStyle(0x8d6e63, 1);
    g.fillRoundedRect(px3 - 42, gy - 60, 84, 60, { tl: 8, tr: 8, bl: 0, br: 0 });
    // Clay texture — dark bands
    g.fillStyle(0x6d4c41, 0.6);
    g.fillRoundedRect(px3 - 38, gy - 54, 76, 50, 6);
    // Inner chamber opening
    g.fillStyle(0x3e2723, 1);
    g.fillEllipse(px3, gy - 38, 48, 24);
    g.fillStyle(0x2d1f1a, 0.9);
    g.fillEllipse(px3, gy - 38, 38, 18);
    // Fire with multiple flame layers
    g.fillStyle(0xbf360c, 0.9);
    g.fillEllipse(px3, gy - 36, 38, 18);
    g.fillStyle(0xff6f00, 0.9);
    g.fillEllipse(px3, gy - 38, 28, 14);
    g.fillStyle(0xff8a00, 0.85);
    g.fillEllipse(px3, gy - 39, 20, 10);
    g.fillStyle(0xffca28, 0.9);
    g.fillEllipse(px3, gy - 40, 12, 6);
    g.fillStyle(0xfff176, 0.85);
    g.fillEllipse(px3, gy - 41, 6, 3);
    // Ember glow around fire mouth
    g.fillStyle(0xff4500, 0.3);
    g.fillEllipse(px3, gy - 32, 50, 20);

    // Pot sitting on top
    g.fillStyle(0x795548, 0.95);
    g.fillEllipse(px3, gy - 65, 95, 36);
    g.fillStyle(0x6d4c41, 0.95);
    g.fillRect(px3 - 48, gy - 108, 96, 44);
    g.fillStyle(0x5d4037, 0.95);
    g.fillEllipse(px3, gy - 108, 95, 36);
    // Pot neck
    g.fillStyle(0x795548, 0.9);
    g.fillRect(px3 - 20, gy - 118, 40, 12);
    g.fillStyle(0x6d4c41, 0.9);
    g.fillEllipse(px3, gy - 118, 40, 14);
    // Pot handles — arc curves
    g.lineStyle(5, 0x4e342e, 1);
    g.beginPath();
    g.arc(px3 - 58, gy - 88, 22, 0.3, Math.PI - 0.3, false);
    g.strokePath();
    g.beginPath();
    g.arc(px3 + 58, gy - 88, 22, 0.3, Math.PI - 0.3, false);
    g.strokePath();
    // Chimney on top
    g.fillStyle(0x795548, 1);
    g.fillRect(px3 - 14, gy - 150, 28, 36);
    g.fillStyle(0x6d4c41, 1);
    g.fillEllipse(px3, gy - 150, 28, 12);
    // Smoke wisps
    g.fillStyle(0x9e9e9e, 0.18);
    for (let i = 0; i < 5; i++) {
      const sx = px3 + 6 + Math.sin(i * 1.4) * 10;
      const sy = gy - 155 - i * 18;
      g.fillEllipse(sx, sy, 14 - i * 2, 12 - i * 1.5);
    }
  }

  drawAnt() {
    this.antContainer = this.add.container(this.antX, this.groundY - 14);
    this.antBodyGroup = this.add.graphics();
    this.antLegsGroup = this.add.graphics();
    this.antAntennaeGroup = this.add.graphics();
    this.antContainer.add(this.antBodyGroup);
    this.antContainer.add(this.antLegsGroup);
    this.antContainer.add(this.antAntennaeGroup);

    // Body segments drawn once
    this.drawAntBody(this.antBodyGroup);
    this.antLegPhase = 0;
  }

  drawAntBody(g) {
    g.clear();
    // Abdomen (large back segment) — dark brown with sheen
    g.fillStyle(0x4a2c0a, 1);
    g.fillEllipse(16, 2, 26, 18);
    g.fillStyle(0x5d3a1a, 1);
    g.fillEllipse(16, 1, 24, 16);
    // Abdomen sheen
    g.fillStyle(0x7a5030, 0.45);
    g.fillEllipse(10, -4, 12, 7);
    // Abdomen segment lines
    g.lineStyle(1, 0x3a1c00, 0.4);
    g.beginPath(); g.moveTo(4, 0); g.lineTo(6, -8); g.strokePath();
    g.beginPath(); g.moveTo(10, 0); g.lineTo(12, -9); g.strokePath();
    g.beginPath(); g.moveTo(16, 0); g.lineTo(18, -8); g.strokePath();

    // Thorax (middle segment)
    g.fillStyle(0x5d3a1a, 1);
    g.fillEllipse(0, 0, 16, 12);
    g.fillStyle(0x6b4423, 0.8);
    g.fillEllipse(0, -1, 14, 10);
    // Thorax sheen
    g.fillStyle(0x8a6040, 0.35);
    g.fillEllipse(-3, -4, 7, 4);

    // Head
    g.fillStyle(0x5d3a1a, 1);
    g.fillCircle(-11, 0, 10);
    g.fillStyle(0x6b4423, 0.8);
    g.fillCircle(-11, -1, 9);
    // Head sheen
    g.fillStyle(0x8a6040, 0.4);
    g.fillEllipse(-14, -4, 5, 3);

    // Eyes — expressive, large for character
    g.fillStyle(0xffd700, 1);
    g.fillCircle(-14, -3, 4.5);
    g.fillStyle(0x1a0a00, 1);
    g.fillCircle(-15, -3, 2.5);
    // Eye highlight
    g.fillStyle(0xffffff, 0.85);
    g.fillCircle(-16, -4.5, 1.2);

    // Mandibles
    g.fillStyle(0x4a2c0a, 1);
    g.fillEllipse(-18, 4, 6, 3);
    g.fillEllipse(-17, 6, 5, 2.5);
    g.fillStyle(0x6b4423, 0.7);
    g.fillEllipse(-18.5, 3.5, 4, 2);

    // Antennae base points
    g.fillStyle(0x5d3a1a, 1);
    g.fillCircle(-8, -8, 3);
    g.fillCircle(-14, -8, 3);
  }

  updateAntVisuals(time) {
    if (!this.antBodyGroup) return;
    const t = time * 0.001;

    // Animate antennae — gentle bobbing
    const a1x = -8 + Math.sin(t * 2.8) * 2;
    const a1y = -8 + Math.cos(t * 2.2) * 1.5;
    const a2x = -14 + Math.sin(t * 2.5 + 1) * 2;
    const a2y = -8 + Math.cos(t * 2.0 + 1) * 1.5;

    this.antAntennaeGroup.clear();
    // Left antenna
    this.antAntennaeGroup.lineStyle(2, 0x5d3a1a, 1);
    this.antAntennaeGroup.beginPath();
    this.antAntennaeGroup.moveTo(-8, -8);
    this.antAntennaeGroup.lineTo(a1x - 4, a1y - 14);
    this.antAntennaeGroup.strokePath();
    this.antAntennaeGroup.fillStyle(0x5d3a1a, 1);
    this.antAntennaeGroup.fillCircle(a1x - 4, a1y - 14, 2);
    // Right antenna
    this.antAntennaeGroup.lineStyle(2, 0x5d3a1a, 1);
    this.antAntennaeGroup.beginPath();
    this.antAntennaeGroup.moveTo(-14, -8);
    this.antAntennaeGroup.lineTo(a2x + 4, a2y - 14);
    this.antAntennaeGroup.strokePath();
    this.antAntennaeGroup.fillStyle(0x5d3a1a, 1);
    this.antAntennaeGroup.fillCircle(a2x + 4, a2y - 14, 2);

    // Animate legs when walking
    if (this.antWalking) {
      this.antLegPhase += 0.18;
      const lp = this.antLegPhase;
      // 6 legs: 3 left (x offsets: -4, 0, 5), 3 right (x offsets: -4, 0, 5)
      // Left legs swing forward when right legs swing back
      this.antLegsGroup.clear();
      this.antLegsGroup.lineStyle(2, 0x4a2c0a, 1);

      // Left legs (swing forward on odd steps)
      [[-6, -1], [0, 0], [5, 1]].forEach(([lx, phase], i) => {
        const swing = Math.sin(lp + phase * 1.2) * 6;
        const jointX = lx + 1;
        const jointY = 2;
        const footX = jointX - 5 + swing;
        const footY = jointY + 13;
        this.antLegsGroup.beginPath();
        this.antLegsGroup.moveTo(lx, 4);
        this.antLegsGroup.lineTo(jointX, jointY);
        this.antLegsGroup.lineTo(footX, footY);
        this.antLegsGroup.strokePath();
      });

      // Right legs (opposite phase)
      [[-6, -1], [0, 0], [5, 1]].forEach(([lx, phase], i) => {
        const swing = Math.sin(lp + phase * 1.2 + Math.PI) * 6;
        const jointX = lx + 1;
        const jointY = 2;
        const footX = jointX - 5 + swing;
        const footY = jointY + 13;
        this.antLegsGroup.beginPath();
        this.antLegsGroup.moveTo(lx, 4);
        this.antLegsGroup.lineTo(jointX, jointY);
        this.antLegsGroup.lineTo(footX, footY);
        this.antLegsGroup.strokePath();
      });
    } else {
      // Idle — legs at rest position
      this.antLegsGroup.clear();
      this.antLegsGroup.lineStyle(2, 0x4a2c0a, 1);
      [[-6], [0], [5]].forEach(([lx]) => {
        this.antLegsGroup.beginPath();
        this.antLegsGroup.moveTo(lx, 4);
        this.antLegsGroup.lineTo(lx + 1, 6);
        this.antLegsGroup.lineTo(lx - 4, 15);
        this.antLegsGroup.strokePath();
      });
      [[-6], [0], [5]].forEach(([lx]) => {
        this.antLegsGroup.beginPath();
        this.antLegsGroup.moveTo(lx, 4);
        this.antLegsGroup.lineTo(lx + 1, 6);
        this.antLegsGroup.lineTo(lx + 6, 15);
        this.antLegsGroup.strokePath();
      });
    }

    // Subtle body bob when walking
    if (this.antWalking) {
      const bob = Math.sin(this.antLegPhase * 2) * 1.5;
      this.antBodyGroup.setY(bob);
    } else {
      this.antBodyGroup.setY(0);
    }
  }

  setupHotspots() {
    const hw = this.width, gy = this.groundY;
    this.hsData = [
      { key: 'food-roti', label: 'Punjab Meal', x: hw * 0.18, y: gy - 10, r: 55,
        prompt: this.topic.hotspots[0].prompt, answer: this.topic.hotspots[0].answer,
        detail: this.topic.hotspots[0].detail },
      { key: 'food-idli', label: 'South Indian Meal', x: hw * 0.5, y: gy - 10, r: 58,
        prompt: this.topic.hotspots[1].prompt, answer: this.topic.hotspots[1].answer,
        detail: this.topic.hotspots[1].detail },
      { key: 'food-chulha', label: 'Traditional Cooker', x: hw * 0.78, y: gy - 32, r: 55,
        prompt: this.topic.hotspots[2].prompt, answer: this.topic.hotspots[2].answer,
        detail: this.topic.hotspots[2].detail },
    ];
    this.hsGraphics = this.add.graphics().setDepth(5);
    this.hsButtons = this.hsData.map((hs) => {
      const btn = this.add.circle(hs.x, hs.y, hs.r)
        .setInteractive({ useHandCursor: true }).setAlpha(0).setDepth(5);
      btn.hsKey = hs.key;
      btn.hsIndex = this.hsData.indexOf(hs);
      btn.on('pointerover', () => { if (btn.alpha > 0.05) btn.setAlpha(0.25); });
      btn.on('pointerout', () => { if (btn.alpha > 0.05) btn.setAlpha(0); });
      return btn;
    });
    this.updateHotspotVisibility();
  }

  updateHotspotVisibility() {
    if (this.closeupOpen) return;
    const state = this.state;
    this.hsGraphics.clear();
    this.hsData.forEach((hs, i) => {
      const btn = this.hsButtons[i];
      const answered = state.answeredKeys.includes(hs.key);
      const isNext = !answered && i === state.currentHotspotIndex;
      if (isNext || answered) {
        const pulse = answered ? 1 : (0.7 + 0.3 * Math.sin(this.time.now * 0.004));
        const alpha = answered ? 0.4 : pulse * 0.85;
        const color = answered ? 0x50c878 : 0xffd60a;
        const lw = 4;
        this.hsGraphics.lineStyle(lw, color, alpha);
        this.hsGraphics.strokeCircle(hs.x, hs.y, hs.r + 6);
        btn.setAlpha(answered ? 0 : pulse * 0.15);
      } else {
        btn.setAlpha(0);
      }
    });
  }

  handleTap(ptr) {
    if (!this.state || this.state.completed || this.closeupOpen) return;
    const nextHS = this.hsData[this.state.currentHotspotIndex];
    if (!nextHS) return;
    const dx = ptr.x - nextHS.x, dy = ptr.y - nextHS.y;
    if (Math.sqrt(dx * dx + dy * dy) <= nextHS.r + 16) {
      this.triggerHotspot(nextHS);
    }
  }

  triggerHotspot(hs) {
    if (this.state.bubbleStage === 'prompt' || this.state.bubbleStage === 'answer') return;
    this.state.currentHotspotKey = hs.key;
    this.state.bubbleStage = 'prompt';
    this.state.bubbleText = hs.prompt;
    this.antTargetX = hs.x;
    this.antWalking = true;
    this.updateBubbleUI();

    // ── CINEMATIC CLOSE-UP TRANSITION ────────────────────────────────────────
    this.time.delayedCall(300, () => {
      this.openCloseup(hs);
    });
  }

  // ─── CLOSE-UP SYSTEM ───────────────────────────────────────────────────────

  openCloseup(hs) {
    this.closeupOpen = true;
    this.currentCloseupHs = hs;
    this.closeupPhase = 'prompt'; // 'prompt' | 'answer' | 'done'

    const w = this.width, h = this.height;
    const cx = w * 0.5, cy = h * 0.44;
    const stageW = Math.min(w * 0.92, 860);
    const stageH = Math.min(h * 0.72, 520);

    // Deep dim overlay
    this.closeupBg = this.add.graphics().setDepth(50).setAlpha(0);
    this.closeupBg.fillStyle(0x000000, 0.88);
    this.closeupBg.fillRect(0, 0, w, h);

    // Animated radial vignette — spotlight ring
    this.closeupVignette = this.add.graphics().setDepth(51).setAlpha(0);
    this.drawVignette(cx, cy, Math.max(stageW, stageH) * 0.7, hs.key);

    // Stage card
    this.closeupStage = this.add.container(cx, cy).setDepth(52).setAlpha(0);
    this.closeupStage.setScale(0.85, 0.85);

    // Background
    const cardBg = this.add.graphics();
    const bgColor = this.closeupBgColor(hs.key);
    cardBg.fillStyle(bgColor, 1);
    cardBg.fillRoundedRect(-stageW / 2, -stageH / 2, stageW, stageH, 28);
    this.closeupStage.add(cardBg);

    // Spotlight glow on card
    const spotlight = this.add.graphics();
    spotlight.fillStyle(0xffffff, 0.06);
    spotlight.fillCircle(0, -stageH * 0.1, stageW * 0.42);
    this.closeupStage.add(spotlight);

    // Draw the dish / scene inside
    this.drawCloseupDish(hs, stageW, stageH);

    // Ingredient badges at bottom
    this.drawIngredientBadges(hs, stageW, stageH);

    // Label at top of card
    const labelText = this.add.text(0, -stageH / 2 + 28, hs.label.toUpperCase(), {
      fontFamily: 'Nunito, sans-serif',
      fontSize: Math.max(13, Math.min(18, w * 0.022)) + 'px',
      color: '#ffd166',
      fontStyle: 'bold',
      letterSpacing: 3,
    }).setOrigin(0.5, 0).setDepth(1);
    this.closeupStage.add(labelText);

    // Divider under label
    const divider = this.add.graphics();
    divider.lineStyle(1, 0xffd166, 0.4);
    divider.beginPath();
    divider.moveTo(-stageW * 0.38, -stageH / 2 + 54);
    divider.lineTo(stageW * 0.38, -stageH / 2 + 54);
    divider.strokePath();
    this.closeupStage.add(divider);

    // Floating particles for atmosphere
    this.drawCloseupParticles(hs.key);

    // Speech bubble (hidden until prompt)
    this.closeupBubble = this.add.container(0, stageH * 0.28).setDepth(53).setAlpha(0);
    this.drawCloseupBubble(hs.prompt, stageW);

    // Tap-to-continue hint
    this.closeupContinueHint = this.add.text(0, stageH / 2 - 18, 'Tap to continue', {
      fontFamily: 'Nunito, sans-serif',
      fontSize: Math.max(11, Math.min(14, w * 0.016)) + 'px',
      color: 'rgba(255,255,255,0.45)',
    }).setOrigin(0.5).setDepth(54).setAlpha(0);

    // Make stage interactive for tap-to-continue
    const hitArea = this.add.rectangle(cx, cy, stageW, stageH)
      .setDepth(49).setAlpha(0).setInteractive({ useHandCursor: true });
    hitArea.on('pointerdown', () => this.onCloseupTap());
    hitArea.on('pointerover', () => { if (this.closeupContinueHint) this.closeupContinueHint.setAlpha(1); });
    hitArea.on('pointerout', () => { if (this.closeupContinueHint) this.closeupContinueHint.setAlpha(0); });

    // ── ANIMATE IN ─────────────────────────────────────────────────────────────
    this.tweens.add({ targets: this.closeupBg, alpha: 1, duration: 420, ease: 'Quad.easeOut' });
    this.tweens.add({ targets: this.closeupVignette, alpha: 1, duration: 500, ease: 'Quad.easeOut' });
    this.tweens.add({ targets: this.closeupStage, alpha: 1, scaleX: 1, scaleY: 1, duration: 480, ease: 'Back.easeOut' });
    this.tweens.add({ targets: this.closeupBubble, alpha: 1, duration: 350, ease: 'Quad.easeOut', delay: 320 });
    this.tweens.add({ targets: hitArea, alpha: 1, duration: 350, delay: 320 });
    this.tweens.add({ targets: this.closeupContinueHint, alpha: 1, duration: 300, delay: 1200 });

    // Pulse the spotlight ring
    this.tweens.add({
      targets: this.closeupVignette, alpha: 0.85, duration: 1200, yoyo: true, repeat: -1, ease: 'Sine.easeInOut'
    });

    if (this.instructionEl) this.instructionEl.textContent = `Think for a moment... Tap to see Anty explain ${hs.label.toLowerCase()}.`;
  }

  drawVignette(cx, cy, r, key) {
    const g = this.closeupVignette;
    if (!g) return;
    const colors = { 'food-roti': [0xffd166, 0xff8a5b], 'food-idli': [0x4fc3f7, 0x29b6f6], 'food-chulha': [0xff6f00, 0xffca28] };
    const [c1, c2] = colors[key] || [0xffd166, 0xff8a5b];
    // Outer soft ring
    g.fillStyle(c1, 0.08);
    g.fillCircle(cx, cy, r);
    g.fillStyle(c2, 0.05);
    g.fillCircle(cx, cy, r * 0.7);
    g.fillStyle(0xffffff, 0.04);
    g.fillCircle(cx, cy, r * 0.35);
  }

  closeupBgColor(key) {
    const m = { 'food-roti': 0x1a0e05, 'food-idli': 0x05120f, 'food-chulha': 0x100802 };
    return m[key] || 0x0a1520;
  }

  drawCloseupDish(hs, stageW, stageH) {
    const g = this.add.graphics();
    const dishX = 0;
    const dishY = -stageH * 0.06;
    const time = this.time ? this.time.now : 0;

    if (hs.key === 'food-roti') {
      // ── PUNJAB MEAL CLOSE-UP ───────────────────────────────────────────────
      // Warm golden atmosphere — wheat field motif

      // Background atmosphere — warm golden glow behind dish
      g.fillStyle(0xff8c00, 0.07);
      g.fillCircle(dishX - 20, dishY + 10, 160);
      g.fillStyle(0xffd700, 0.05);
      g.fillCircle(dishX - 20, dishY + 10, 120);

      // Large banana leaf — multiple layers for depth
      g.fillStyle(0x1b5e20, 0.95);
      g.fillEllipse(dishX, dishY + 48, 380, 100);
      g.fillStyle(0x2e7d32, 0.88);
      g.fillEllipse(dishX, dishY + 44, 355, 88);
      g.fillStyle(0x388e3c, 0.75);
      g.fillEllipse(dishX, dishY + 40, 320, 75);
      // Leaf veins — radiating from center
      g.lineStyle(1.5, 0x1b5e20, 0.5);
      for (let i = 0; i < 12; i++) {
        const angle = (i / 12) * Math.PI * 2;
        const ex = dishX + Math.cos(angle) * 160;
        const ey = dishY + 42 + Math.sin(angle) * 40;
        g.beginPath(); g.moveTo(dishX, dishY + 42); g.lineTo(ex, ey); g.strokePath();
      }
      // Leaf edge darkening
      g.fillStyle(0x1b5e20, 0.3);
      g.fillEllipse(dishX, dishY + 44, 380, 100);

      // Plate shadow
      g.fillStyle(0x000000, 0.12);
      g.fillEllipse(dishX, dishY + 36, 290, 45);

      // Plate
      g.fillStyle(0xf5deb3, 0.95);
      g.fillEllipse(dishX, dishY + 30, 280, 55);
      g.fillStyle(0xe8c89a, 0.85);
      g.fillEllipse(dishX, dishY + 27, 255, 46);
      // Plate rim
      g.lineStyle(3, 0xd4a870, 0.4);
      g.strokeEllipse(dishX, dishY + 30, 280, 55);

      // Makki di Roti — 3 thick, rustic rotis with char texture
      const rotiData = [
        { ox: -65, oy: 8,  rot: -0.22, c1: 0xf5c518, c2: 0xd4a010, chars: [[-18,-4],[8,2],[-5,6]] },
        { ox: 0,   oy: 2,  rot:  0.05, c1: 0xf0c040, c2: 0xc49020, chars: [[-22,2],[12,5],[0,8]] },
        { ox: 62,  oy: 10, rot:  0.18, c1: 0xe8b020, c2: 0xb88010, chars: [[-15,3],[18,-2],[5,7]] },
      ];
      rotiData.forEach(({ ox, oy, rot, c1, c2, chars }) => {
        // Roti body — thick corn bread look
        g.fillStyle(c2, 0.4);
        g.fillEllipse(dishX + ox + 3, dishY + oy + 2, 145, 38);
        g.fillStyle(c1, 0.97);
        g.fillEllipse(dishX + ox, dishY + oy, 145, 38);
        // Roti highlight (top-left)
        g.fillStyle(0xfff0a0, 0.3);
        g.fillEllipse(dishX + ox - 20, dishY + oy - 10, 55, 14);
        // Roti edge shadow
        g.fillStyle(c2, 0.35);
        g.fillEllipse(dishX + ox + 3, dishY + oy + 3, 138, 34);
        // Char marks — dark brown/black burnt spots
        chars.forEach(([cx, cyy], i) => {
          g.fillStyle(0x6b4000, 0.8);
          g.fillCircle(dishX + ox + cx, dishY + oy + cyy, 9 + i);
          g.fillStyle(0x4a2800, 0.6);
          g.fillCircle(dishX + ox + cx + 1, dishY + oy + cyy + 1, 6 + i);
          // Char highlight
          g.fillStyle(0xcc8800, 0.3);
          g.fillCircle(dishX + ox + cx - 2, dishY + oy + cyy - 2, 3);
        });
      });

      // Sarson da Saag — lush multi-layered green bowl
      g.fillStyle(0x1b5e20, 0.97);
      g.fillEllipse(dishX - 80, dishY + 22, 125, 48);
      g.fillStyle(0x2e7d32, 0.92);
      g.fillEllipse(dishX - 80, dishY + 18, 112, 42);
      g.fillStyle(0x388e3c, 0.87);
      g.fillEllipse(dishX - 80, dishY + 14, 96, 35);
      g.fillStyle(0x43a047, 0.8);
      g.fillEllipse(dishX - 80, dishY + 10, 76, 27);
      g.fillStyle(0x4caf50, 0.7);
      g.fillEllipse(dishX - 80, dishY + 6, 55, 19);
      // Saag texture swirls — darker patches
      g.fillStyle(0x1b5e20, 0.4);
      g.fillEllipse(dishX - 92, dishY + 8, 32, 14);
      g.fillEllipse(dishX - 68, dishY + 5, 24, 10);
      g.fillEllipse(dishX - 80, dishY + 12, 18, 8);

      // Ghee dollops — bright with specular highlight
      const gheePos = [[-100, 0], [-78, -3], [-90, -8]];
      gheePos.forEach(([gx, gy2]) => {
        g.fillStyle(0xffd700, 0.95);
        g.fillCircle(dishX + gx, dishY + gy2, 12);
        g.fillStyle(0xffe044, 0.7);
        g.fillCircle(dishX + gx - 2, dishY + gy2 - 2, 8);
        g.fillStyle(0xffffff, 0.55);
        g.fillCircle(dishX + gx - 4, dishY + gy2 - 5, 3);
      });

      // Onion rings — sliced cross-section
      g.fillStyle(0xf5e6d0, 0.9);
      g.fillEllipse(dishX + 115, dishY + 8, 62, 26);
      g.fillStyle(0xdcc8b0, 0.8);
      g.fillEllipse(dishX + 115, dishY + 8, 52, 21);
      g.fillStyle(0xc8a898, 0.7);
      g.fillEllipse(dishX + 115, dishY + 8, 42, 17);
      g.fillStyle(0xf8f0e0, 0.85);
      g.fillEllipse(dishX + 115, dishY + 8, 32, 13);
      // Ring layers
      g.lineStyle(2, 0xe0c8b8, 0.6);
      g.strokeEllipse(dishX + 115, dishY + 8, 48, 19);
      g.strokeEllipse(dishX + 115, dishY + 8, 36, 14);
      g.lineStyle(1, 0xe0c8b8, 0.4);
      g.strokeEllipse(dishX + 115, dishY + 8, 24, 10);

      // Butter pat with wrapper
      g.fillStyle(0xf0e060, 0.85);
      g.fillRoundedRect(dishX + 100, dishY - 8, 34, 22, 5);
      g.fillStyle(0xfff176, 0.95);
      g.fillRoundedRect(dishX + 103, dishY - 5, 28, 16, 3);
      g.fillStyle(0xffffff, 0.4);
      g.fillEllipse(dishX + 108, dishY - 2, 8, 4);
      // Butter wrapper fold lines
      g.lineStyle(1, 0xd4c030, 0.5);
      g.beginPath(); g.moveTo(dishX + 100, dishY - 8); g.lineTo(dishX + 134, dishY - 8); g.strokePath();

      // Wheat stalk decorations at edges
      g.lineStyle(2.5, 0xd4a030, 0.7);
      [[dishX - 170, dishY + 20], [dishX + 165, dishY + 20]].forEach(([wx, wy]) => {
        g.beginPath(); g.moveTo(wx, wy + 30); g.lineTo(wx, wy - 20); g.strokePath();
        for (let k = 0; k < 5; k++) {
          const kx = wx + (k % 2 === 0 ? -12 : 12);
          const ky = wy - 5 - k * 8;
          g.beginPath(); g.moveTo(wx, wy - k * 7); g.lineTo(kx, ky); g.strokePath();
        }
      });

    } else if (hs.key === 'food-idli') {
      // ── SOUTH INDIAN MEAL CLOSE-UP ─────────────────────────────────────────
      // Cool misty atmosphere

      // Background atmosphere — cool blue-teal glow
      g.fillStyle(0x006064, 0.08);
      g.fillCircle(dishX + 10, dishY + 10, 170);
      g.fillStyle(0x00838f, 0.05);
      g.fillCircle(dishX + 10, dishY + 10, 130);

      // Banana leaf
      g.fillStyle(0x1b5e20, 0.95);
      g.fillEllipse(dishX, dishY + 50, 400, 105);
      g.fillStyle(0x2e7d32, 0.88);
      g.fillEllipse(dishX, dishY + 46, 372, 92);
      g.fillStyle(0x388e3c, 0.75);
      g.fillEllipse(dishX, dishY + 42, 340, 80);
      g.lineStyle(1.5, 0x1b5e20, 0.45);
      for (let i = 0; i < 14; i++) {
        const angle = (i / 14) * Math.PI * 2;
        const ex = dishX + Math.cos(angle) * 170;
        const ey = dishY + 44 + Math.sin(angle) * 44;
        g.beginPath(); g.moveTo(dishX, dishY + 44); g.lineTo(ex, ey); g.strokePath();
      }
      g.fillStyle(0x1b5e20, 0.25);
      g.fillEllipse(dishX, dishY + 48, 400, 105);

      // Plate
      g.fillStyle(0xf5deb3, 0.95);
      g.fillEllipse(dishX, dishY + 32, 300, 58);
      g.fillStyle(0xe8c89a, 0.85);
      g.fillEllipse(dishX, dishY + 29, 274, 49);
      g.lineStyle(3, 0xd4a870, 0.4);
      g.strokeEllipse(dishX, dishY + 32, 300, 58);

      // Idli plate — 3 fluffy idlis with detailed texture
      const idliData = [[-80, 0], [0, -10], [80, 0]];
      idliData.forEach(([ox, oy]) => {
        // Plate rim for idlis
        g.fillStyle(0xd4c4a8, 0.8);
        g.fillEllipse(dishX + ox, dishY + oy + 16, 90, 16);
        // Idli base shadow
        g.fillStyle(0xc8b898, 0.85);
        g.fillEllipse(dishX + ox, dishY + oy + 12, 88, 34);
        // Idli body — fluffy white
        g.fillStyle(0xf5f5dc, 0.97);
        g.fillEllipse(dishX + ox, dishY + oy + 6, 84, 32);
        g.fillStyle(0xfafaf0, 0.93);
        g.fillEllipse(dishX + ox, dishY + oy + 2, 76, 26);
        // Idli dome — lighter top
        g.fillStyle(0xffffff, 0.6);
        g.fillEllipse(dishX + ox - 5, dishY + oy - 4, 40, 14);
        // Idli texture pores
        g.fillStyle(0xe8e0c0, 0.4);
        for (let p = 0; p < 6; p++) {
          const px = dishX + ox + (p - 2.5) * 14;
          const py = dishY + oy + 4 + (p % 2) * 6;
          g.fillCircle(px, py, 3);
        }
        // Steam wisps
        g.fillStyle(0xffffff, 0.14);
        g.fillEllipse(dishX + ox - 12, dishY + oy - 28, 12, 22);
        g.fillEllipse(dishX + ox + 10, dishY + oy - 34, 10, 18);
        g.fillEllipse(dishX + ox, dishY + oy - 22, 8, 14);
      });

      // Sambhar — rich orange-red bowl
      g.fillStyle(0xbf360c, 0.9);
      g.fillEllipse(dishX + 105, dishY + 18, 100, 44);
      g.fillStyle(0xcB4422, 0.85);
      g.fillEllipse(dishX + 105, dishY + 14, 90, 38);
      g.fillStyle(0xd84315, 0.8);
      g.fillEllipse(dishX + 105, dishY + 10, 78, 32);
      g.fillStyle(0xe64a19, 0.7);
      g.fillEllipse(dishX + 105, dishY + 6, 65, 26);
      // Sambhar surface — oily shine
      g.fillStyle(0xff7043, 0.3);
      g.fillEllipse(dishX + 105, dishY + 2, 50, 18);
      g.fillStyle(0xffab91, 0.2);
      g.fillEllipse(dishX + 100, dishY, 28, 10);
      // Veggies visible in sambhar
      g.fillStyle(0x8bc34a, 0.85);
      g.fillEllipse(dishX + 88, dishY + 4, 18, 11); // drumstick
      g.fillStyle(0xff9800, 0.8);
      g.fillEllipse(dishX + 112, dishY + 6, 14, 9); // carrot
      g.fillStyle(0xe65100, 0.75);
      g.fillEllipse(dishX + 100, dishY + 2, 12, 8); // pumpkin
      g.fillStyle(0x795548, 0.7);
      g.fillEllipse(dishX + 122, dishY + 4, 8, 6); // tamarind

      // Coconut chutney — white with green-red swirls
      g.fillStyle(0xf5f5dc, 0.9);
      g.fillEllipse(dishX - 115, dishY + 20, 82, 34);
      g.fillStyle(0xfffde7, 0.82);
      g.fillEllipse(dishX - 115, dishY + 17, 70, 28);
      // Chutney texture — coconut shreds
      g.fillStyle(0xffffff, 0.6);
      for (let s = 0; s < 8; s++) {
        const sx = dishX - 125 + s * 14 + (s % 2) * 6;
        const sy = dishY + 14 + (s % 3) * 6;
        g.fillEllipse(sx, sy, 10, 4);
      }
      // Green chutney swirl
      g.fillStyle(0x388e3c, 0.7);
      g.fillEllipse(dishX - 122, dishY + 14, 28, 15);
      g.fillStyle(0x2e7d32, 0.6);
      g.fillEllipse(dishX - 118, dishY + 12, 18, 10);
      // Red chutney dot
      g.fillStyle(0xc62828, 0.65);
      g.fillCircle(dishX - 108, dishY + 18, 7);
      g.fillStyle(0xff5252, 0.4);
      g.fillCircle(dishX - 110, dishY + 16, 3);

      // Papad — crispy and golden with spice coating
      g.fillStyle(0xf5d020, 0.88);
      g.fillEllipse(dishX + 15, dishY + 40, 105, 22);
      g.fillStyle(0xe8c030, 0.7);
      g.fillEllipse(dishX + 15, dishY + 40, 82, 16);
      // Papad texture — bubbled surface
      g.fillStyle(0xd4a820, 0.4);
      for (let b = 0; b < 5; b++) {
        g.fillCircle(dishX + 5 + b * 18, dishY + 38, 5 + b % 2 * 3);
      }
      // Black pepper specks
      g.fillStyle(0x3d2800, 0.5);
      for (let p = 0; p < 6; p++) {
        g.fillCircle(dishX - 10 + p * 20, dishY + 38 + (p % 2) * 4, 1.5);
      }

    } else if (hs.key === 'food-chulha') {
      // ── TRADITIONAL CHULHA CLOSE-UP ───────────────────────────────────────
      // Warm fire glow atmosphere

      // Fire glow — atmospheric radiance behind the chulha
      g.fillStyle(0xff4500, 0.1);
      g.fillCircle(dishX, dishY + 20, 200);
      g.fillStyle(0xff6f00, 0.08);
      g.fillCircle(dishX, dishY + 10, 160);
      g.fillStyle(0xffca28, 0.06);
      g.fillCircle(dishX, dishY, 110);

      // Ground / hearth base
      g.fillStyle(0x4e342e, 0.9);
      g.fillRoundedRect(dishX - 110, dishY + 60, 220, 40, { tl: 0, tr: 0, bl: 16, br: 16 });
      // Ash bed
      g.fillStyle(0x795548, 0.8);
      g.fillEllipse(dishX, dishY + 58, 180, 28);

      // Clay body — main cylinder with texture bands
      g.fillStyle(0x8d6e63, 1);
      g.fillRoundedRect(dishX - 95, dishY - 40, 190, 105, { tl: 16, tr: 16, bl: 0, br: 0 });
      // Clay texture — horizontal banding
      g.fillStyle(0x6d4c41, 0.5);
      g.fillRect(dishX - 90, dishY - 30, 180, 8);
      g.fillRect(dishX - 90, dishY - 10, 180, 6);
      g.fillRect(dishX - 90, dishY + 8, 180, 6);
      g.fillRect(dishX - 90, dishY + 24, 180, 6);
      // Clay cracks and roughness
      g.fillStyle(0x5d4037, 0.35);
      g.fillEllipse(dishX - 50, dishY - 15, 30, 20);
      g.fillEllipse(dishX + 60, dishY + 5, 25, 18);
      // Clay highlight
      g.fillStyle(0xa08070, 0.3);
      g.fillRoundedRect(dishX - 85, dishY - 35, 40, 90, 8);

      // Inner fire chamber — deep dark opening
      g.fillStyle(0x2d1f1a, 1);
      g.fillEllipse(dishX, dishY - 8, 80, 40);
      g.fillStyle(0x1a0d08, 0.95);
      g.fillEllipse(dishX, dishY - 8, 65, 32);

      // Fire — animated multi-layered flames
      const ft = time * 0.001;
      // Back flame (deep red/orange)
      g.fillStyle(0xbf360c, 0.9);
      g.fillEllipse(dishX, dishY - 4, 55, 28);
      // Middle flames
      g.fillStyle(0xff6f00, 0.92);
      g.fillEllipse(dishX, dishY - 7, 42, 22);
      g.fillStyle(0xff8a00, 0.88);
      g.fillEllipse(dishX, dishY - 9, 30, 15);
      // Inner bright flame
      g.fillStyle(0xffca28, 0.95);
      g.fillEllipse(dishX, dishY - 11, 18, 10);
      g.fillStyle(0xfff176, 0.9);
      g.fillEllipse(dishX, dishY - 12, 10, 5);

      // Animated flame tongues
      for (let i = 0; i < 5; i++) {
        const fx = dishX + (i - 2) * 16;
        const fh = 18 + Math.sin(ft * 4 + i * 1.3) * 10;
        g.fillStyle(0xff8a5b, 0.5 + Math.sin(ft * 3 + i) * 0.15);
        g.fillTriangle(fx - 6, dishY - 14, fx + 6, dishY - 14, fx, dishY - 14 - fh);
      }

      // Ember glow around mouth
      g.fillStyle(0xff4500, 0.25);
      g.fillEllipse(dishX, dishY, 75, 30);
      g.fillStyle(0xff6f00, 0.15);
      g.fillEllipse(dishX, dishY - 2, 55, 22);

      // Fuel wood at base
      const woodColors = [0x5d4037, 0x6d4c41, 0x4e342e, 0x795548];
      for (let i = 0; i < 5; i++) {
        const wx = dishX - 75 + i * 38;
        const ww = 6 + (i % 2) * 2;
        const wh = 35 + (i % 3) * 10;
        g.fillStyle(woodColors[i % woodColors.length], 0.9);
        g.fillRoundedRect(wx, dishY + 55, ww, wh, 3);
        // Wood grain
        g.fillStyle(0x3e2a1e, 0.3);
        g.fillRect(wx + 1, dishY + 57, 2, wh - 4);
      }

      // Pot on top
      g.fillStyle(0x795548, 0.97);
      g.fillEllipse(dishX, dishY - 60, 110, 42);
      g.fillStyle(0x6d4c41, 0.95);
      g.fillRect(dishX - 55, dishY - 115, 110, 56);
      g.fillStyle(0x5d4037, 0.97);
      g.fillEllipse(dishX, dishY - 115, 110, 42);
      // Pot neck
      g.fillStyle(0x795548, 0.92);
      g.fillRect(dishX - 22, dishY - 126, 44, 14);
      g.fillStyle(0x6d4c41, 0.9);
      g.fillEllipse(dishX, dishY - 126, 44, 15);
      // Pot lid slightly open
      g.fillStyle(0x795548, 0.88);
      g.fillEllipse(dishX, dishY - 132, 50, 16);
      g.fillStyle(0x6d4c41, 0.85);
      g.fillEllipse(dishX, dishY - 134, 45, 13);
      // Steam from pot
      for (let i = 0; i < 4; i++) {
        const sx = dishX - 20 + i * 28 + Math.sin(ft * 1.5 + i * 0.9) * 8;
        const sy = dishY - 138 - i * 18;
        g.fillStyle(0xcfd8dc, 0.12 + i * 0.03);
        g.fillEllipse(sx, sy, 14 - i * 2, 12 - i * 1.5);
      }
      // Pot handles
      g.lineStyle(5, 0x4e342e, 1);
      g.beginPath(); g.arc(dishX - 65, dishY - 95, 24, 0.25, Math.PI - 0.25, false); g.strokePath();
      g.beginPath(); g.arc(dishX + 65, dishY - 95, 24, 0.25, Math.PI - 0.25, false); g.strokePath();

      // Chimney with animated smoke
      g.fillStyle(0x795548, 1);
      g.fillRect(dishX - 18, dishY - 185, 36, 55);
      g.fillStyle(0x6d4c41, 1);
      g.fillEllipse(dishX, dishY - 185, 36, 14);
      g.fillStyle(0x5d4037, 0.8);
      g.fillEllipse(dishX, dishY - 185, 28, 10);
      // Animated smoke
      for (let i = 0; i < 5; i++) {
        const sy = dishY - 188 - i * 22;
        const sx = dishX + 8 + Math.sin(ft * 1.2 + i * 1.1) * 14;
        g.fillStyle(0x9e9e9e, 0.12 + (4 - i) * 0.03);
        g.fillEllipse(sx, sy, 16 - i * 2, 14 - i * 1.5);
      }
    }

    this.closeupStage.add(g);
  }

  drawIngredientBadges(hs, stageW, stageH) {
    this.ingredientBadges = [];
    const badges = this.getBadges(hs.key);
    const bw = Math.min(130, stageW * 0.18);
    const bh = 32;
    const gap = 10;
    const totalW = badges.length * bw + (badges.length - 1) * gap;
    let bx = -totalW / 2;
    const by = stageH / 2 - bh - 10;

    badges.forEach((label, i) => {
      const bg = this.add.graphics();
      bg.fillStyle(0xffffff, 0.12);
      bg.fillRoundedRect(bx, by, bw, bh, bh / 2);
      bg.lineStyle(1.5, 0xffffff, 0.25);
      bg.strokeRoundedRect(bx, by, bw, bh, bh / 2);
      const txt = this.add.text(bx + bw / 2, by + bh / 2, label, {
        fontFamily: 'Nunito, sans-serif',
        fontSize: Math.max(10, Math.min(13, stageW * 0.016)) + 'px',
        color: '#ffffff',
        fontStyle: 'bold',
      }).setOrigin(0.5).setDepth(1);
      this.closeupStage.add(bg);
      this.closeupStage.add(txt);
      this.ingredientBadges.push(bg, txt);
      bx += bw + gap;
    });
  }

  getBadges(key) {
    if (key === 'food-roti') return ['Makki di Roti', 'Sarson da Saag', 'Ghee', 'Local Wheat'];
    if (key === 'food-idli') return ['Idli', 'Sambhar', 'Chutney', 'Rice & Urad Dal'];
    if (key === 'food-chulha') return ['Clay Body', 'Biomass Fuel', 'Traditional', 'Slow Cooking'];
    return [];
  }

  drawCloseupParticles(key) {
    const colors = { 'food-roti': [0xffd166, 0xf0c040, 0xff8a5b], 'food-idli': [0x4fc3f7, 0x80deea, 0xffffff], 'food-chulha': [0xff6f00, 0xffca28, 0xff8a5b] };
    const palette = colors[key] || colors['food-roti'];
    this.particleTimers = [];

    for (let i = 0; i < 18; i++) {
      const px = Phaser.Math.Between(-this.width * 0.38, this.width * 0.38);
      const py = Phaser.Math.Between(-this.height * 0.32, this.height * 0.25);
      const r = Phaser.Math.FloatBetween(1.5, 4);
      const c = Phaser.Math.RND.pick(palette);
      const p = this.add.circle(px, py, r, c, Phaser.Math.FloatBetween(0.2, 0.5)).setDepth(53);
      const dur = Phaser.Math.Between(1800, 3500);
      const yDist = Phaser.Math.Between(30, 80);
      this.tweens.add({
        targets: p, y: py - yDist, alpha: 0,
        duration: dur, delay: Phaser.Math.Between(0, 1200),
        ease: 'Sine.easeOut', onComplete: () => p.destroy()
      });
      this.tweens.add({
        targets: p, x: px + Phaser.Math.Between(-20, 20),
        duration: dur, delay: Phaser.Math.Between(0, 800),
        ease: 'Sine.easeInOut'
      });
    }
  }

  drawCloseupBubble(text, stageW) {
    if (this.closeupBubble) this.closeupBubble.removeAll(true);

    const bw = Math.min(stageW * 0.88, 680);
    const bh = 110;
    const bx = 0, by = 0;

    const bubble = this.add.graphics();
    // Bubble body
    bubble.fillStyle(0x0d1b2a, 0.92);
    bubble.fillRoundedRect(bx - bw / 2, by, bw, bh, 20);
    // Border
    bubble.lineStyle(2, 0xffd166, 0.5);
    bubble.strokeRoundedRect(bx - bw / 2, by, bw, bh, 20);
    // Tail pointing up toward dish
    bubble.fillStyle(0x0d1b2a, 0.92);
    bubble.fillTriangle(bx - 14, by, bx + 14, by, bx, by - 24);
    bubble.lineStyle(2, 0xffd166, 0.5);
    bubble.beginPath();
    bubble.moveTo(bx - 14, by);
    bubble.lineTo(bx, by - 24);
    bubble.lineTo(bx + 14, by);
    bubble.strokePath();

    // Anty label
    const antTag = this.add.text(bx - bw / 2 + 18, by + 14, 'ANTY SAYS', {
      fontFamily: 'Nunito, sans-serif',
      fontSize: '11px',
      color: '#ffd166',
      fontStyle: 'bold',
      letterSpacing: 2,
    }).setDepth(1);

    // Word-split text for typewriter effect
    const words = text.trim().split(/\s+/);
    const displayText = this.add.text(bx - bw / 2 + 18, by + 34, '', {
      fontFamily: 'Nunito, sans-serif',
      fontSize: Math.max(13, Math.min(17, this.width * 0.022)) + 'px',
      color: '#ffffff',
      wordWrap: { width: bw - 36 },
      lineSpacing: 4,
    }).setDepth(1);

    this.closeupBubble.add(bubble);
    this.closeupBubble.add(antTag);
    this.closeupBubble.add(displayText);

    // Typewriter reveal
    this.typewriterIndex = 0;
    this.typewriterWords = words;
    this.typewriterTarget = displayText;
    this.typewriterInterval = this.time.addEvent({
      delay: 55,
      callback: () => this.onTypewriterTick(),
      repeat: words.length
    });
  }

  onTypewriterTick() {
    if (!this.typewriterTarget || !this.typewriterWords) return;
    if (this.typewriterIndex >= this.typewriterWords.length) return;
    this.typewriterTarget.text += (this.typewriterIndex > 0 ? ' ' : '') + this.typewriterWords[this.typewriterIndex];
    this.typewriterIndex++;
  }

  onCloseupTap() {
    if (this.closeupPhase === 'prompt') {
      this.showAnswer();
    } else if (this.closeupPhase === 'answer') {
      this.closeCloseup();
    }
  }

  showAnswer() {
    if (this.closeupPhase !== 'prompt') return;
    this.closeupPhase = 'answer';
    const hs = this.currentCloseupHs;

    // Fade out prompt bubble
    if (this.closeupBubble) {
      this.tweens.add({ targets: this.closeupBubble, alpha: 0, duration: 200 });
    }

    // Draw answer bubble
    this.time.delayedCall(250, () => {
      if (this.closeupBubble) this.closeupBubble.removeAll(true);
      this.drawAnswerBubble(hs, Math.min(this.width * 0.92, 860));
    });

    if (this.instructionEl) this.instructionEl.textContent = `Good thinking! Here's Anty's explanation about ${hs.label.toLowerCase()}.`;
  }

  drawAnswerBubble(hs, stageW) {
    if (!this.closeupStage) return;
    const bw = Math.min(stageW * 0.88, 680);
    const bh = 110;
    const by = this.height * 0.28;

    const bubble = this.add.graphics();
    bubble.fillStyle(0x0a2a1a, 0.94);
    bubble.fillRoundedRect(-bw / 2, by, bw, bh, 20);
    bubble.lineStyle(2, 0x50c878, 0.6);
    bubble.strokeRoundedRect(-bw / 2, by, bw, bh, 20);
    // Tail
    bubble.fillStyle(0x0a2a1a, 0.94);
    bubble.fillTriangle(-14, by, 14, by, 0, by - 24);
    bubble.lineStyle(2, 0x50c878, 0.6);
    bubble.beginPath();
    bubble.moveTo(-14, by); bubble.lineTo(0, by - 24); bubble.lineTo(14, by);
    bubble.strokePath();

    const antTag = this.add.text(-bw / 2 + 18, by + 14, 'ANTY EXPLAINS', {
      fontFamily: 'Nunito, sans-serif',
      fontSize: '11px',
      color: '#50c878',
      fontStyle: 'bold',
      letterSpacing: 2,
    }).setDepth(1);

    const words = (hs.answer || '').trim().split(/\s+/);
    const displayText = this.add.text(-bw / 2 + 18, by + 34, '', {
      fontFamily: 'Nunito, sans-serif',
      fontSize: Math.max(13, Math.min(17, this.width * 0.022)) + 'px',
      color: '#ffffff',
      wordWrap: { width: bw - 36 },
      lineSpacing: 4,
    }).setDepth(1);

    this.closeupBubble = this.add.container(0, 0).setDepth(53);
    this.closeupBubble.add(bubble);
    this.closeupBubble.add(antTag);
    this.closeupBubble.add(displayText);

    this.tweens.add({ targets: this.closeupBubble, alpha: 1, duration: 300 });
    this.tweens.add({ targets: this.closeupContinueHint, alpha: 1, duration: 200 });

    // Typewriter for answer
    this.typewriterIndex = 0;
    this.typewriterWords = words;
    this.typewriterTarget = displayText;
    this.typewriterInterval = this.time.addEvent({
      delay: 48,
      callback: () => this.onTypewriterTick(),
      repeat: words.length
    });
  }

  closeCloseup() {
    if (!this.closeupOpen) return;
    this.closeupPhase = 'done';

    // Cleanup typewriter
    if (this.typewriterInterval) { this.typewriterInterval.destroy(); this.typewriterInterval = null; }
    if (this.particleTimers) this.particleTimers.forEach(t => t.destroy());

    // Animate out
    const outTween = { targets: [this.closeupBg, this.closeupVignette, this.closeupStage, this.closeupBubble, this.closeupContinueHint], alpha: 0, duration: 380, ease: 'Quad.easeIn' };
    this.tweens.add(outTween);

    this.time.delayedCall(400, () => {
      if (this.closeupBg) { this.closeupBg.destroy(); this.closeupBg = null; }
      if (this.closeupVignette) { this.closeupVignette.destroy(); this.closeupVignette = null; }
      if (this.closeupStage) { this.closeupStage.removeAll(true); this.closeupStage.destroy(); this.closeupStage = null; }
      if (this.closeupBubble) { this.closeupBubble.removeAll(true); this.closeupBubble.destroy(); this.closeupBubble = null; }
      if (this.closeupContinueHint) { this.closeupContinueHint.destroy(); this.closeupContinueHint = null; }
      this.closeupOpen = false;
      this.currentCloseupHs = null;
      this.ingredientBadges = [];

      // Finish the hotspot from original flow
      if (this.currentCloseupHs_obj) {
        this.finishHotspot(this.currentCloseupHs_obj);
        this.currentCloseupHs_obj = null;
      } else {
        this._afterCloseupFinish();
      }
    });
  }

  _afterCloseupFinish() {
    // Restore bubble UI from state
    this.updateBubbleUI();
    // Restore instruction
    const hs = this.hsData[this.state.currentHotspotIndex];
    if (this.instructionEl && hs) this.instructionEl.textContent = `Next, tap ${hs.label.toLowerCase()}.`;
    this.updateHotspotVisibility();
  }

  triggerHotspot(hs) {
    if (this.state.bubbleStage === 'prompt' || this.state.bubbleStage === 'answer') return;
    this.state.currentHotspotKey = hs.key;
    this.state.bubbleStage = 'prompt';
    this.state.bubbleText = hs.prompt;
    this.antTargetX = hs.x;
    this.antWalking = true;
    this.updateBubbleUI();
    this.time.delayedCall(300, () => { this.openCloseup(hs); });
  }

  finishHotspot(hs) {
    const state = this.state;
    if (!state.answeredKeys.includes(hs.key)) state.answeredKeys.push(hs.key);
    state.currentHotspotIndex = state.answeredKeys.length;
    state.currentHotspotKey = null;
    this.antWalking = false;
    if (state.answeredKeys.length >= this.hsData.length) {
      state.completed = true;
      state.bubbleText = 'Nice work! You explored every item. Now go to the check section.';
      state.bubbleStage = 'done';
      if (this.instructionEl) this.instructionEl.textContent = '';
    } else {
      state.bubbleStage = 'idle';
      state.bubbleText = 'Tap the next glowing item to continue.';
      const nextHS = this.hsData[state.currentHotspotIndex];
      if (this.instructionEl && nextHS) this.instructionEl.textContent = `Next, tap ${nextHS.label.toLowerCase()}.`;
    }
    this.updateBubbleUI();
    this.updateHotspotVisibility();
  }

  updateBubbleUI() {
    const state = this.state;
    if (!state) return;
    if (this.bubbleEl) this.bubbleEl.textContent = state.bubbleText || '';
    if (this.detailPanel) this.detailPanel.classList.remove('selection-detail-panel--visible');
  }

  update(time, delta) {
    advanceAnimationFlow(this.topic);

    if (this.antWalking) {
      const dx = this.antTargetX - this.antX;
      if (Math.abs(dx) > this.antSpeed) {
        this.antX += Math.sign(dx) * this.antSpeed;
      } else {
        this.antX = this.antTargetX;
        this.antWalking = false;
      }
      this.antContainer.x = this.antX;
    }

    // Update ant animation — body bob, leg walk cycle, antennae sway
    this.updateAntVisuals(time);

    if (!this.closeupOpen) {
      this.updateHotspotVisibility();
    }

    // Pulse spotlight in closeup
    if (this.closeupVignette && this.closeupOpen) {
      // handled by tween
    }
  }
}

function startPhaserForTopic1() {
  if (phaserGame) return;
  try {
    phaserGame = new Phaser.Game({
      type: Phaser.AUTO,
      parent: 'phaser-container',
      width: window.innerWidth,
      height: window.innerHeight,
      backgroundColor: '#0d1b2a',
      scene: [PhaserFoodDiversityScene],
      render: { antialias: true, pixelArt: false },
      scale: { mode: Phaser.Scale.RESIZE, autoCenter: Phaser.Scale.CENTER_BOTH }
    });
    document.getElementById('phaser-container').style.display = 'block';
  } catch (e) {
    console.warn('Phaser failed to start, falling back to p5.js:', e);
    phaserGame = null;
  }
}

function stopPhaser() {
  if (!phaserGame) return;
  phaserGame.destroy(true);
  phaserGame = null;
  document.getElementById('phaser-container').style.display = 'none';
}
