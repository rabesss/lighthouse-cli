# Changelog

## 0.1.0 (2026-09-23)


### ⚠ BREAKING CHANGES

* **auth:** Removes Playwright dependency and browser-based auth. Users must now provide credentials via CLI flags, env vars, or prompts.

### Features

* 7-change auth migration — CDP-first, auto-refresh, persistent context ([eef997d](https://github.com/rabesss/lighthouse-cli/commit/eef997d40cd58c215b5b1fe3baa6d8ef30954c34))
* add assessment workflow foundations ([e5bd9a0](https://github.com/rabesss/lighthouse-cli/commit/e5bd9a03afa6007210de38b3fbbafae760c272a4))
* add checkpointed instructor quiz previews ([e2f3ece](https://github.com/rabesss/lighthouse-cli/commit/e2f3ece4eecca16824e0d44352f666e54332533e))
* add checkpointed trial quiz previews ([39a7a0d](https://github.com/rabesss/lighthouse-cli/commit/39a7a0da5266f35c00b4b59830d1e4116145cf3f))
* Add droid-review.yml workflow ([b78c9ef](https://github.com/rabesss/lighthouse-cli/commit/b78c9ef2cb4862b7058d1802428afab61b09727f))
* Add droid.yml workflow ([5249a24](https://github.com/rabesss/lighthouse-cli/commit/5249a24f374e63e1decb0dce7cecef22e3c12118))
* add role-oriented assessment workflows ([f4b9842](https://github.com/rabesss/lighthouse-cli/commit/f4b98424127adc48e2eb81371f0bab6bebdda7c3))
* add site-scoped transport and lazy CLI loading ([db114a2](https://github.com/rabesss/lighthouse-cli/commit/db114a2bc543f9fe76836642d2837bb93a544f88))
* add teacher and student assessment workflows ([1e3c425](https://github.com/rabesss/lighthouse-cli/commit/1e3c425dee0c312f182dd14b23a4b89d9ff20d8e))
* **auth:** ConvergedTFA SAS flow with two-phase login and MFA options ([75114b4](https://github.com/rabesss/lighthouse-cli/commit/75114b45963228e08293ff8fb45ca22bc494f7ee))
* **auth:** replace Playwright with pure HTTP Microsoft SSO client ([ac36d9c](https://github.com/rabesss/lighthouse-cli/commit/ac36d9cd5fd68843c351a09892af23eb996d5da3))
* **auth:** two-step MFA verify, KMSI/SAML fixes, and SSO docs ([ac78ded](https://github.com/rabesss/lighthouse-cli/commit/ac78ded26293a49e5f167571bdc9948fab936790))


### Bug Fixes

* address CodeRabbit review findings ([0783f3d](https://github.com/rabesss/lighthouse-cli/commit/0783f3d170edd972629d02de8a18faf00090c1ae))
* **auth:** address PR [#5](https://github.com/rabesss/lighthouse-cli/issues/5) review (pending-session match + input prompts to stderr) ([0cc74df](https://github.com/rabesss/lighthouse-cli/commit/0cc74df7fef0190587d96ab906324d43d0d3ddd3))
* **auth:** address PR review — cookies, MFA pending, and UX ([6dbf6e4](https://github.com/rabesss/lighthouse-cli/commit/6dbf6e4e0f79a628af1670fe5de559abae6344ec))
* **auth:** address unattended PR [#4](https://github.com/rabesss/lighthouse-cli/issues/4) review comments ([75759f6](https://github.com/rabesss/lighthouse-cli/commit/75759f6ef4b35b215490659cb3c398eb740a9899))
* **auth:** handle list-valued class attr in MS error parsing ([e8ec509](https://github.com/rabesss/lighthouse-cli/commit/e8ec509ea5e2d1f30120181ed6fe7d075c7fe777))
* **auth:** SAML login flow — $Config parse, token hydration, GCT preflight ([ad3363c](https://github.com/rabesss/lighthouse-cli/commit/ad3363c08b96010e3f404005ac714dec3d6fa18e))
* harden assessment routes and ambiguous statuses ([2ef3c2b](https://github.com/rabesss/lighthouse-cli/commit/2ef3c2b78f49ab8bfdc0249b78b9804525e9fa13))
* harden preview recovery and JSON boundaries ([36ae44e](https://github.com/rabesss/lighthouse-cli/commit/36ae44ec669406c6dae1d86eaa9ed920dcb57473))
* keep assignment submission compatible without CSRF bootstrap ([5cab43c](https://github.com/rabesss/lighthouse-cli/commit/5cab43c2e478cd065a6601209342efb4b14d0e45))
* keep CSRF bootstrap failures retryable ([bc0f880](https://github.com/rabesss/lighthouse-cli/commit/bc0f880bd5cb93a1e19a9fb78d549aff0b5c27ba))
* keep file submission on the cookie-only fast path ([bf40ae9](https://github.com/rabesss/lighthouse-cli/commit/bf40ae9021f1aa3d11c97379e71f38ba5cb702a8))
* mark preview state uncertain before process GET ([6de0751](https://github.com/rabesss/lighthouse-cli/commit/6de075199320db2330da1393e192c422d4ba2cce))
* preserve assessment JSON and write outcome contracts ([f5c519b](https://github.com/rabesss/lighthouse-cli/commit/f5c519bf35a5a1dfdcd21ebfab38496b9fc1c8e6))
* preserve uncertain preview starts after auth loss ([2079f33](https://github.com/rabesss/lighthouse-cli/commit/2079f3325b49c56238754ca60d412725c3cd79eb))
* preserve uncertain preview writes after auth expiry ([c1dd70b](https://github.com/rabesss/lighthouse-cli/commit/c1dd70b563cbe800aa4947487ad5e5317686dad6))
* preserve uncertain writes after auth loss ([c0c4176](https://github.com/rabesss/lighthouse-cli/commit/c0c4176c0adc7d380005726c29976f64e024ddee))
* preserve unknown assessment writes on session expiry ([a9fa240](https://github.com/rabesss/lighthouse-cli/commit/a9fa240f55eced57c789939f91dcbdf811fb6fe1))
* probe preview cursor after uncertain navigation ([ef9023d](https://github.com/rabesss/lighthouse-cli/commit/ef9023d657b0940a9d5405f240e0358ff9d1a9c4))
* recover preview navigation without replay ([5075653](https://github.com/rabesss/lighthouse-cli/commit/5075653767d9053592f8849e3393a74da1471fda))
* recoverable, exactly-once quiz preview starts and working live submit ([#45](https://github.com/rabesss/lighthouse-cli/issues/45)) ([9f971ab](https://github.com/rabesss/lighthouse-cli/commit/9f971ab23698e6df341f8e3f8eb8c63b348bdd73))
* register isolated session import command ([691d6b0](https://github.com/rabesss/lighthouse-cli/commit/691d6b0f3c9f157227e35c7389299015f2436fd2))
* use the D2L LE classlist route ([e9da7e6](https://github.com/rabesss/lighthouse-cli/commit/e9da7e6e3e89e07078a942a2d9f4cbf9d29dd37d))


### Documentation

* add contributing guide ([c271af7](https://github.com/rabesss/lighthouse-cli/commit/c271af767e9f841916dea67c13e5c1e685b91bdf))
* add Copilot + Greptile review configs; REVIEW.md (uppercase) for Kilo ([fa97acb](https://github.com/rabesss/lighthouse-cli/commit/fa97acb1a6f705f09252ee84aadcfe16eaf1ba8a))
* add MIT license ([5d949e1](https://github.com/rabesss/lighthouse-cli/commit/5d949e1119e76c89faee986b6797b4accdecd72f))
* add security policy ([08f607c](https://github.com/rabesss/lighthouse-cli/commit/08f607c6bc7858d0537600e360054b7e457d15d5))
* AI code-review configs for all reviewer bots + doc refresh ([4d2c21a](https://github.com/rabesss/lighthouse-cli/commit/4d2c21a8d0ecd28288da31406371ebc0462c5c67))
* align uncertain navigation recovery guidance ([6bd32be](https://github.com/rabesss/lighthouse-cli/commit/6bd32be4d2defe390c2ba2f9e81b080b47701972))
* clarify cookie-only file submission ([62457b4](https://github.com/rabesss/lighthouse-cli/commit/62457b4118deb68af873a5eedd24a9c1bbf6dc04))
* clarify optional submission CSRF ([2be418c](https://github.com/rabesss/lighthouse-cli/commit/2be418c245ef31c2866e79a38e097ff74951ed6e))
* document assessment and preview workflows ([81b924b](https://github.com/rabesss/lighthouse-cli/commit/81b924bef5720be97c6b2693736bde41c825b6ec))
* record final auth recovery validation ([6a8bab9](https://github.com/rabesss/lighthouse-cli/commit/6a8bab91b295178674ce3d26068c5b781e5aef13))
* record final external-review validation ([d7d321e](https://github.com/rabesss/lighthouse-cli/commit/d7d321e94a246443a9d6369414258aac70e75b83))
* record final layered validation counts ([65f1822](https://github.com/rabesss/lighthouse-cli/commit/65f182237a2c55ce41fc20a94752c345b5a24149))
* record final preview auth recovery tests ([1920dd3](https://github.com/rabesss/lighthouse-cli/commit/1920dd3f8e6715f0e4364b3eacee0ef6217dfb23))
* record hardened preview validation ([5ec4c61](https://github.com/rabesss/lighthouse-cli/commit/5ec4c612805539b96ba5c2e2102998c377609f22))
* record observed final suite timing ([d2881ac](https://github.com/rabesss/lighthouse-cli/commit/d2881aca217c54683c0f556f343e2dd1c22a1b8e))
