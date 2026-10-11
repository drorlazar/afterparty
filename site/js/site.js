/* Afterparty landing page. Vanilla JS, no dependencies.
   Theme toggle, copy-the-prompt, film player with chapter chips, habit accordion clips, mobile install bar. */
(function () {
  'use strict';

  var PROMPT = 'Install Afterparty from https://github.com/drorlazar/afterparty — read INSTALL.md and follow it.';
  var THEME_KEY = 'afterparty-theme';
  // Built-in copy of assets/film/chapters.json, used only if that file can't be fetched.
  var FALLBACK_CHAPTERS = {
    duration: 56.85,
    chapters: [
      { id: 'laptop', start: 0.0, end: 5.81, title: 'The laptop', kind: 'story' },
      { id: 'dive', start: 5.81, end: 7.02, title: 'Into the drive', kind: 'story' },
      { id: 'hook', start: 7.02, end: 8.47, title: 'Disk full', kind: 'mess' },
      { id: 'rewind', start: 8.47, end: 9.92, title: 'Rewind', kind: 'story' },
      { id: 'venue', start: 9.92, end: 13.79, title: 'The party starts', kind: 'story' },
      { id: 'guests', start: 13.79, end: 18.15, title: 'Render, install, branch', kind: 'mess' },
      { id: 'mess', start: 18.15, end: 22.02, title: 'The mess', kind: 'mess' },
      { id: 'scratch', start: 22.02, end: 23.95, title: 'Record scratch', kind: 'mess' },
      { id: 'afterparty', start: 23.95, end: 27.58, title: 'Meet Afterparty', kind: 'afterparty' },
      { id: 'habit-1', start: 27.58, end: 30.0, title: 'One bin', kind: 'afterparty', habit: 1 },
      { id: 'habit-2', start: 30.0, end: 32.9, title: '/tidy', kind: 'afterparty', habit: 2 },
      { id: 'habit-3', start: 32.9, end: 36.77, title: 'Sunday sweep', kind: 'afterparty', habit: 3 },
      { id: 'habit-4', start: 36.77, end: 43.06, title: 'The map', kind: 'afterparty', habit: 4 },
      { id: 'promise', start: 43.06, end: 50.32, title: 'The promise', kind: 'afterparty' },
      { id: 'encore', start: 50.32, end: 51.53, title: 'Encore', kind: 'story' },
      { id: 'sunrise', start: 51.53, end: 56.85, title: 'Sunrise', kind: 'story' }
    ]
  };

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  // chapters.json: the chapter list (generated from the film timeline). Resolves to null if it can't be read.
  var mediaReady = fetch('assets/film/chapters.json', { cache: 'no-cache' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .catch(function () { return null; });
  var live = document.getElementById('live');
  function announce(msg) { if (!live) return; live.textContent = ''; setTimeout(function () { live.textContent = msg; }, 30); }
  function fmt(t) { t = Math.max(0, Math.floor(t || 0)); return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0'); }

  /* ---------- Theme: system / light / dark ---------- */
  function initTheme() {
    var buttons = document.querySelectorAll('[data-theme-choice]');
    var saved = 'system';
    try { var v = localStorage.getItem(THEME_KEY); if (v === 'light' || v === 'dark') saved = v; } catch (e) {}
    function apply(choice) {
      var root = document.documentElement;
      if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
      else root.removeAttribute('data-theme');
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-theme-choice') === choice)); });
    }
    apply(saved);
    buttons.forEach(function (b) {
      b.addEventListener('click', function () {
        var choice = b.getAttribute('data-theme-choice');
        apply(choice);
        try { if (choice === 'system') localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, choice); } catch (e) {}
      });
    });
  }

  /* ---------- Copy the install prompt ---------- */
  function legacyCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.left = '0'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  function selectPromptText() {
    var el = document.getElementById('install-prompt');
    if (!el || !window.getSelection) return;
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  function copyPrompt() {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(PROMPT).then(function () { return true; }, function () { return legacyCopy(PROMPT); });
    }
    return Promise.resolve(legacyCopy(PROMPT));
  }
  function initCopy() {
    document.querySelectorAll('[data-copy-prompt]').forEach(function (btn) {
      var label = btn.querySelector('.btn__label');
      var original = label ? label.getAttribute('data-label') : '';
      var timer;
      btn.addEventListener('click', function () {
        copyPrompt().then(function (ok) {
          clearTimeout(timer);
          if (ok) {
            btn.classList.remove('is-copied'); void btn.offsetWidth; btn.classList.add('is-copied');
            if (label) label.textContent = 'Copied';
            announce('Install prompt copied. Paste it into Claude Code.');
          } else {
            selectPromptText();
            var install = document.getElementById('install');
            if (install && btn.closest('#install') === null) install.scrollIntoView({ block: 'center' });
            if (label) label.textContent = 'Press ⌘C to copy';
            announce('Prompt selected. Press Command C to copy.');
          }
          timer = setTimeout(function () {
            btn.classList.remove('is-copied');
            if (label) label.textContent = original;
          }, 2200);
        });
      });
    });
  }

  /* ---------- Film ---------- */
  function initFilm() {
    var film = document.getElementById('film');
    if (!film) return;
    var video = document.getElementById('film-video');
    var list = document.getElementById('chapters');
    var input = film.querySelector('.scrub__input');
    var fill = film.querySelector('.scrub__fill');
    var ticks = film.querySelector('.scrub__ticks');
    var nowEl = film.querySelector('[data-time-now]');
    var totalEl = film.querySelector('[data-time-total]');
    var toggles = film.querySelectorAll('[data-film-toggle]');
    var soundBtn = film.querySelector('[data-sound]');
    var ccBtn = film.querySelector('[data-captions]');

    var data = FALLBACK_CHAPTERS;
    var chips = [];
    var activeIndex = -1;
    var hasVideo = false;
    var videoFailed = false;
    var userPaused = false;
    var startedByUs = false;
    // Without a video file the playhead is simulated, so chapters still work.
    var virtual = { t: 0, playing: false, last: 0, raf: 0 };

    function duration() {
      return hasVideo && isFinite(video.duration) && video.duration > 0 ? video.duration : data.duration;
    }
    function currentTime() { return hasVideo ? video.currentTime : virtual.t; }
    function isPlaying() { return hasVideo ? !video.paused && !video.ended : virtual.playing; }

    function renderChapters() {
      list.innerHTML = '';
      ticks.innerHTML = '';
      chips = data.chapters.map(function (c, i) {
        var li = document.createElement('li');
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'chip';
        b.setAttribute('data-kind', c.kind || 'story');
        b.setAttribute('data-chapter', c.id);
        b.setAttribute('aria-current', 'false');
        // The accessible name starts with "Chapter N:" and then repeats the visible text exactly.
        b.innerHTML = '<span class="sr">Chapter ' + (i + 1) + ': </span><span class="chip__dot" aria-hidden="true"></span>' +
          '<span class="chip__time">' + fmt(c.start) + '</span> <span class="chip__label"></span>';
        b.querySelector('.chip__label').textContent = c.title;
        b.addEventListener('click', function () { seekChapter(c.id, true); });
        li.appendChild(b);
        list.appendChild(li);
        if (i > 0) {
          var tick = document.createElement('i');
          tick.style.left = (c.start / data.duration * 100) + '%';
          ticks.appendChild(tick);
        }
        return b;
      });
      input.max = String(data.duration);
      totalEl.textContent = fmt(data.duration);
      update();
    }

    function chapterIndexAt(t) {
      for (var i = data.chapters.length - 1; i >= 0; i--) if (t >= data.chapters[i].start) return i;
      return 0;
    }

    function keepChipInView(chip) {
      var l = list, cl = chip.offsetLeft, cw = chip.offsetWidth;
      if (cl < l.scrollLeft + 12 || cl + cw > l.scrollLeft + l.clientWidth - 28) {
        var target = cl - (l.clientWidth - cw) / 2;
        l.scrollTo({ left: Math.max(0, target), behavior: reduceMotion.matches ? 'auto' : 'smooth' });
      }
    }

    function update() {
      var t = currentTime();
      var d = duration();
      var frac = d ? Math.min(1, t / d) : 0;
      fill.style.width = (frac * 100) + '%';
      if (document.activeElement !== input) input.value = String(t);
      input.setAttribute('aria-valuetext', fmt(t) + ' of ' + fmt(d));
      nowEl.textContent = fmt(t);
      var i = chapterIndexAt(t);
      var c = data.chapters[i];
      if (i !== activeIndex) {
        if (chips[activeIndex]) { chips[activeIndex].setAttribute('aria-current', 'false'); chips[activeIndex].style.removeProperty('--p'); }
        var first = activeIndex === -1;
        activeIndex = i;
        if (chips[i]) { chips[i].setAttribute('aria-current', 'true'); if (!first) keepChipInView(chips[i]); }
      }
      if (chips[i] && c) chips[i].style.setProperty('--p', Math.min(1, Math.max(0, (t - c.start) / (c.end - c.start))).toFixed(3));
      film.classList.toggle('is-playing', isPlaying());
      toggles.forEach(function (b) { b.setAttribute('aria-label', isPlaying() ? 'Pause film' : 'Play film'); });
    }

    function tickVirtual(now) {
      if (!virtual.playing) return;
      var dt = (now - virtual.last) / 1000;
      virtual.last = now;
      virtual.t = Math.min(data.duration, virtual.t + dt);
      if (virtual.t >= data.duration) { virtual.playing = false; }
      update();
      if (virtual.playing) virtual.raf = requestAnimationFrame(tickVirtual);
    }

    function play() {
      if (hasVideo || !videoFailed) {
        var p = video.play();
        if (p && p.catch) p.catch(function (err) {
          if (!hasVideo && err && err.name === 'NotSupportedError') markFailed();
          if (videoFailed) playVirtual();
          update();
        });
      } else {
        playVirtual();
      }
      update();
    }
    function playVirtual() {
      if (virtual.t >= data.duration) virtual.t = 0;
      virtual.playing = true;
      virtual.last = performance.now();
      cancelAnimationFrame(virtual.raf);
      virtual.raf = requestAnimationFrame(tickVirtual);
    }
    function pause() {
      if (hasVideo) video.pause();
      virtual.playing = false;
      cancelAnimationFrame(virtual.raf);
      update();
    }
    function seek(t) {
      t = Math.max(0, Math.min(duration(), t));
      if (hasVideo) { try { video.currentTime = t; } catch (e) {} }
      virtual.t = t;
      update();
    }
    function seekChapter(id, andPlay) {
      var c = data.chapters.filter(function (x) { return x.id === id; })[0];
      if (!c) return;
      seek(c.start + 0.034); // one frame in, so the picture is the chapter's first frame
      if (andPlay && !reduceMotion.matches) { userPaused = false; play(); }
      announce('Chapter: ' + c.title);
    }
    film.seekChapter = seekChapter;

    toggles.forEach(function (b) {
      b.addEventListener('click', function () {
        if (isPlaying()) { userPaused = true; pause(); } else { userPaused = false; play(); }
      });
    });
    input.addEventListener('input', function () { seek(parseFloat(input.value)); });

    // Sound: starts muted (autoplay rule); the toggle unmutes and keeps playing.
    soundBtn.addEventListener('click', function () {
      var on = soundBtn.getAttribute('aria-pressed') !== 'true';
      soundBtn.setAttribute('aria-pressed', String(on));
      soundBtn.setAttribute('aria-label', on ? 'Sound on. Turn sound off' : 'Sound off. Turn sound on');
      video.muted = !on;
      if (on && !isPlaying()) { userPaused = false; play(); }
    });

    // Captions are on by default so the muted film still tells the story.
    function applyCaptions() {
      var on = ccBtn.getAttribute('aria-pressed') === 'true';
      for (var i = 0; i < video.textTracks.length; i++) video.textTracks[i].mode = on ? 'showing' : 'hidden';
    }
    ccBtn.addEventListener('click', function () {
      ccBtn.setAttribute('aria-pressed', String(ccBtn.getAttribute('aria-pressed') !== 'true'));
      applyCaptions();
    });

    // Video lifecycle. A missing file leaves the poster in place and the chips working.
    function markFailed() {
      if (hasVideo || videoFailed) return;
      videoFailed = true;
      film.classList.add('no-video');
      update();
    }
    video.addEventListener('error', markFailed);
    // If neither source can be played (or the files are missing), the poster stays and the chips run on a
    // simulated playhead.
    var sources = video.querySelectorAll('source');
    if (sources.length) sources[sources.length - 1].addEventListener('error', markFailed);
    else markFailed();
    // Metadata is enough to play and seek (preload="metadata"; Safari never fires loadeddata before play).
    video.addEventListener('loadedmetadata', function () {
      var wasPlaying = virtual.playing;
      hasVideo = true;
      film.classList.add('has-video');
      if (virtual.t) { try { video.currentTime = virtual.t; } catch (e) {} }
      virtual.playing = false;
      cancelAnimationFrame(virtual.raf);
      if (isFinite(video.duration)) totalEl.textContent = fmt(video.duration);
      applyCaptions();
      if (wasPlaying) play();
      update();
    });
    ['timeupdate', 'play', 'pause', 'seeked', 'ended'].forEach(function (ev) { video.addEventListener(ev, update); });
    ['playing', 'seeked'].forEach(function (ev) { video.addEventListener(ev, function () { film.classList.add('has-frame'); }); });
    video.addEventListener('timeupdate', function () { if (!video.paused) requestAnimationFrame(update); });

    // Muted autoplay while on screen, unless the visitor prefers reduced motion. It starts only after the
    // page has loaded and the main thread is idle, so video decoding never competes with first paint.
    var idle = window.requestIdleCallback || function (fn) { return setTimeout(fn, 200); };
    function whenSettled(fn) {
      if (document.readyState === 'complete') idle(fn); else window.addEventListener('load', function () { idle(fn); }, { once: true });
    }
    if ('IntersectionObserver' in window) whenSettled(function () {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (reduceMotion.matches || userPaused) return;
          if (e.isIntersecting && e.intersectionRatio >= 0.5) {
            if (!isPlaying() && hasVideo) { startedByUs = true; video.muted = soundBtn.getAttribute('aria-pressed') !== 'true'; play(); }
          } else if (startedByUs && isPlaying()) {
            pause();
          }
        });
      }, { threshold: [0, 0.5] });
      io.observe(film);
    });
    if (reduceMotion.matches) { video.removeAttribute('autoplay'); video.preload = 'none'; }

    // Chapters: chapters.json if present, the built-in copy otherwise.
    renderChapters();
    mediaReady.then(function (json) {
      if (json && Array.isArray(json.chapters) && json.chapters.length) {
        data = { duration: json.duration || json.chapters[json.chapters.length - 1].end, chapters: json.chapters };
        activeIndex = -1;
        renderChapters();
      }
    });

    // "Watch this part in the film" links in the habits accordion.
    document.querySelectorAll('[data-seek-chapter]').forEach(function (b) {
      b.addEventListener('click', function () {
        film.scrollIntoView({ block: 'center', behavior: reduceMotion.matches ? 'auto' : 'smooth' });
        seekChapter(b.getAttribute('data-seek-chapter'), true);
      });
    });
  }

  /* ---------- Habits accordion: one open at a time; the open part plays its clip ---------- */
  function initHabits() {
    var parts = Array.prototype.slice.call(document.querySelectorAll('.part'));
    parts.forEach(function (part) {
      var clip = part.querySelector('.part__clip');
      var v = clip && clip.querySelector('video');
      var ready = false;
      if (v) {
        v.addEventListener('loadeddata', function () { clip.classList.add('has-video'); });
        // The clip (preload="none") loads only when its part is opened.
        ready = v.querySelectorAll('source').length > 0;
      }
      var onScreen = false;
      if (v && 'IntersectionObserver' in window) {
        new IntersectionObserver(function (entries) {
          onScreen = entries[entries.length - 1].isIntersecting;
          sync();
        }, { rootMargin: '100px 0px' }).observe(clip);
      } else {
        onScreen = true;
      }
      function sync() {
        if (!v || !ready) return;
        // Plays only while its part is open and on screen; the clip isn't fetched before that.
        if (part.open && onScreen && !reduceMotion.matches) {
          if (v.preload === 'none') { v.preload = 'auto'; v.load(); }
          var p = v.play(); if (p && p.catch) p.catch(function () {});
        } else {
          v.pause();
        }
      }
      part.addEventListener('toggle', function () {
        if (part.open) parts.forEach(function (o) { if (o !== part && o.open) o.open = false; });
        sync();
      });
      sync();
    });
  }

  /* ---------- Mobile install bar: hidden while another install button is on screen ---------- */
  function initMobileBar() {
    var bar = document.getElementById('mbar');
    if (!bar || !('IntersectionObserver' in window)) return;
    var watched = [document.getElementById('hero-actions'), document.getElementById('install')].filter(Boolean);
    var visible = new Set();
    function set(hidden) {
      bar.classList.toggle('is-hidden', hidden);
      if (hidden) { bar.setAttribute('inert', ''); bar.setAttribute('aria-hidden', 'true'); }
      else { bar.removeAttribute('inert'); bar.removeAttribute('aria-hidden'); }
    }
    set(true);
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) visible.add(e.target); else visible.delete(e.target); });
      set(visible.size > 0);
    });
    watched.forEach(function (el) { io.observe(el); });
  }

  initTheme();
  initCopy();
  initFilm();
  initHabits();
  initMobileBar();
})();
