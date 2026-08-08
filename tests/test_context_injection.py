import sys, unittest, importlib.util
from unittest.mock import patch, MagicMock

sys.path.insert(0, 'scripts')

def _load_ace():
    spec = importlib.util.spec_from_file_location(
        'agent_contribution_engine',
        'scripts/agent-contribution-engine.py'
    )
    ace = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ace)
    return ace

class TestContextInjection(unittest.TestCase):

    def _make_gap(self):
        return {
            'id': 1,
            'repo_full_name': 'BerriAI/litellm',
            'description': 'Missing docstring on func foo',
            'source_url': None,
            'wedge_type': 'missing_documentation',
        }

    def test_contributing_md_in_prompt(self):
        """CONTRIBUTING.md content appears in the built prompt."""
        ace = _load_ace()
        ctx = {
            'contributing_md': 'Please run tests with pytest before submitting.',
            'pr_template': '',
            'merged_prs': [],
            'naming_conventions': '',
            'repo_full_name': 'BerriAI/litellm',
        }
        prompt = ace.build_claude_prompt(self._make_gap(), repo_ctx=ctx)
        self.assertIn('Please run tests with pytest', prompt)

    def test_merged_prs_in_prompt(self):
        """Recently merged PR titles appear in the prompt."""
        ace = _load_ace()
        ctx = {
            'contributing_md': '',
            'pr_template': '',
            'merged_prs': [
                {'title': 'Add Groq model support', 'body': 'Adds groq/llama3', 'diff_excerpt': '+GROQ_LLAMA3 = ...'},
            ],
            'naming_conventions': '',
            'repo_full_name': 'BerriAI/litellm',
        }
        prompt = ace.build_claude_prompt(self._make_gap(), repo_ctx=ctx)
        self.assertIn('Add Groq model support', prompt)

    def test_no_context_still_works(self):
        """build_claude_prompt works fine with no repo_ctx (backwards compat)."""
        ace = _load_ace()
        prompt = ace.build_claude_prompt(self._make_gap())
        self.assertIn('Missing docstring', prompt)

    def test_context_section_heading(self):
        """Prompt contains a clear section heading when context is present."""
        ace = _load_ace()
        ctx = {
            'contributing_md': 'Sign the CLA.',
            'pr_template': '## Summary',
            'merged_prs': [],
            'naming_conventions': 'def snake_case_fn():',
            'repo_full_name': 'BerriAI/litellm',
        }
        prompt = ace.build_claude_prompt(self._make_gap(), repo_ctx=ctx)
        self.assertIn('REPO CONTEXT', prompt)

if __name__ == '__main__':
    unittest.main()
