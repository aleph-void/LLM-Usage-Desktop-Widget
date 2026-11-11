# Website

Source for the marketing site at [llm-usage-widget.alephvoid.com](https://llm-usage-widget.alephvoid.com).

The site is static HTML and CSS with no build step; `styles.css` follows the shared Aleph Void palette used by the other product sites. Preview it locally from the repository root with any static server:

```sh
python3 -m http.server 8000 -d site
```

`assets/screenshots` is a symlink to the repository's `images/` folder, so the README and the site share one set of screenshots. (On Windows, clone with `git config core.symlinks true` to preview them.)

The download buttons link to the GitHub release page and, when JavaScript can reach the GitHub API, are rewritten to point at the latest release's files (`main.js`).

Pushing changes under `site/` or `images/` to `main` deploys to GitHub Pages with `.github/workflows/deploy-site.yml`.
