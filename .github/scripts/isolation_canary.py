"""CI gate: the isolation backend must actually work before any test runs.

Capability packs, chat tools, MFD and the meeting tools all execute through
``pack_sandbox``, which is fail-closed: with no working backend it refuses to
run anything, and dozens of tests fail with one cascading cause. That failure is
correct behaviour, so the fix belongs in the environment, not in the assertions
-- and an environment claim has to be proved rather than assumed. "bubblewrap is
installed" is not the property the code needs; "bubblewrap can create a user
namespace here" is, and AppArmor on newer Ubuntu can deny exactly that while the
package sits happily on disk.

So this runs the real thing, twice over:

* ``sandbox_linux.available()`` -- the harness precondition, which executes a
  minimal ``--unshare-user`` and checks the exit code;
* ``pack_sandbox.probe(refresh=True)`` -- the same canary the product runs,
  which builds a sandbox and verifies from inside it that it cannot write
  outside its output directory, cannot read host files, and has no network
  interface beyond loopback.

Both are imported from the repository, so this cannot drift from what the tests
require: if the product changes what isolation means, this gate changes with it.
Exit code 0 means the backend is real. Anything else stops the job here, where
the reason is one line, instead of in sixty-eight unrelated assertions.

Only stdlib imports are reachable from these two modules, so the gate runs
before the project's dependencies are installed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from factory.control import pack_sandbox  # noqa: E402
from factory.harness import sandbox_linux  # noqa: E402


def main() -> int:
    failures = []

    if sys.platform.startswith('linux'):
        # The harness gate. Its own probe is
        # `/usr/bin/bwrap --unshare-user --ro-bind / / true`.
        binary = Path(sandbox_linux.SANDBOX_BINARY)
        print(f'sandbox_linux.SANDBOX_BINARY = {binary} (exists={binary.exists()})')
        harness_ok = sandbox_linux.available()
        print(f'sandbox_linux.available() = {harness_ok}')
        if not harness_ok:
            failures.append(
                f'{binary} 无法创建 user namespace：bwrap 装了不等于能用，'
                '请检查 runner 的 unprivileged userns / AppArmor 限制')

    # The product's own canary: builds a real sandbox and checks, from inside it,
    # that the boundary holds. This is what the capability-pack tests need.
    backend = pack_sandbox.backend_name()
    print(f'pack_sandbox.backend_name() = {backend}')
    isolation = pack_sandbox.probe(refresh=True)
    print('pack_sandbox.probe() = ' + json.dumps(isolation.as_dict(), ensure_ascii=False))
    if not isolation.available:
        failures.append(f'隔离金丝雀未通过：{isolation.reason}')

    if failures:
        print('\n'.join(['', '隔离前提不满足，测试不会开始：', *failures]), file=sys.stderr)
        return 1
    print('隔离后端可用，继续执行测试。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
