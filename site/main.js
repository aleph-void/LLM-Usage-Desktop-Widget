(function () {
  // Mobile menu
  const menuBtn = document.getElementById('mobile-menu-btn');
  const menu = document.getElementById('mobile-menu');

  if (menuBtn && menu) {
    menuBtn.addEventListener('click', function () {
      menu.classList.toggle('open');
    });

    menu.querySelectorAll('a').forEach(function (a) {
      a.addEventListener('click', function () {
        menu.classList.remove('open');
      });
    });
  }

  // Smooth nav shadow on scroll
  const nav = document.getElementById('nav');
  if (nav) {
    window.addEventListener('scroll', function () {
      nav.style.boxShadow = window.scrollY > 10 ? 'var(--shadow-sm)' : 'none';
    });
  }

  // Screenshot lightbox
  const lightbox = document.getElementById('lightbox');
  const lightboxImg = document.getElementById('lightbox-img');
  const lightboxClose = document.getElementById('lightbox-close');

  if (lightbox && lightboxImg) {
    document.querySelectorAll('.shot img').forEach(function (img) {
      img.addEventListener('click', function () {
        lightboxImg.src = img.src;
        lightboxImg.alt = img.alt;
        lightbox.classList.add('open');
        lightbox.setAttribute('aria-hidden', 'false');
      });
    });

    function closeLightbox() {
      lightbox.classList.remove('open');
      lightbox.setAttribute('aria-hidden', 'true');
      lightboxImg.src = '';
    }

    lightbox.addEventListener('click', function (e) {
      if (e.target === lightbox || e.target === lightboxClose) closeLightbox();
    });
    if (lightboxClose) lightboxClose.addEventListener('click', closeLightbox);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeLightbox();
    });
  }
  // Point the download buttons straight at the latest release's files.
  // Without JavaScript (or if GitHub can't be reached) they open the release page.
  var assetLinks = document.querySelectorAll('[data-asset]');
  if (assetLinks.length && window.fetch) {
    var patterns = {
      'deb': /_all\.deb$/,
      'mac-arm64': /-macos-arm64\.dmg$/,
      'mac-x64': /-macos-x86_64\.dmg$/,
      'win-x64': /-windows-x64-setup\.exe$/,
      'win-arm64': /-windows-arm64-setup\.exe$/
    };
    fetch('https://api.github.com/repos/aleph-void/LLM-Usage-Desktop-Widget/releases/latest', {
      headers: { Accept: 'application/vnd.github+json' }
    })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (release) {
        var assets = release.assets || [];
        assetLinks.forEach(function (a) {
          var re = patterns[a.getAttribute('data-asset')];
          for (var i = 0; re && i < assets.length; i++) {
            var url = assets[i].browser_download_url || '';
            if (re.test(assets[i].name) && url.indexOf('https://github.com/aleph-void/') === 0) {
              a.href = url;
              break;
            }
          }
        });
        var label = document.getElementById('release-version');
        if (label && /^v?[\w.-]+$/.test(release.tag_name || '')) {
          label.textContent = 'Latest: ' + release.tag_name;
        }
      })
      .catch(function () { /* keep the release-page links */ });
  }
})();
