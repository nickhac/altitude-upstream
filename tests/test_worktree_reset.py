import subprocess, sys, os, unittest
from unittest.mock import patch, call, MagicMock

sys.path.insert(0, 'scripts')

class TestWorktreeReset(unittest.TestCase):

    def test_hard_reset_called_on_existing_worktree(self):
        """git clean -fdx and git reset --hard are called when worktree exists."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('agent_contribution_engine', 'scripts/agent-contribution-engine.py')
        ace = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ace)

        calls_made = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = ''
            m.stderr = ''
            return m

        with patch('os.path.exists', return_value=True), \
             patch('subprocess.run', side_effect=fake_run), \
             patch.object(ace, 'ensure_fork', return_value=('https://github.com/nickhac/repo.git', 'nickhac/repo')), \
             patch.object(ace, 'get_fine_grained_pat', return_value='fake-pat'), \
             patch.object(ace, 'get_classic_pat', return_value='fake-pat'), \
             patch('urllib.request.urlopen', side_effect=Exception('no network')):
            try:
                ace.setup_worktree('owner/repo', 'fine-pat', 'classic-pat')
            except Exception:
                pass

        cmds = [' '.join(c) for c in calls_made if isinstance(c, list)]
        self.assertTrue(
            any('clean' in c and '-fdx' in c for c in cmds),
            f"git clean -fdx not called. Called: {cmds}"
        )
        self.assertTrue(
            any('reset' in c and '--hard' in c for c in cmds),
            f"git reset --hard not called. Called: {cmds}"
        )

if __name__ == '__main__':
    unittest.main()
