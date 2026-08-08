# tests/test_repo_context.py
import json, sys, types, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, 'scripts')

class TestGetRepoContext(unittest.TestCase):

    def _mock_http(self, responses: dict):
        """responses maps URL substring → (status, body_bytes)"""
        import urllib.request
        original_urlopen = urllib.request.urlopen

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            for key, (status, body) in responses.items():
                if key in url:
                    m = MagicMock()
                    m.read.return_value = body
                    m.status = status
                    m.__enter__ = lambda s: s
                    m.__exit__ = MagicMock(return_value=False)
                    return m
            raise ValueError(f"Unmocked URL: {url}")

        return patch('urllib.request.urlopen', side_effect=fake_urlopen)

    def test_returns_all_keys(self):
        """get_repo_context always returns all required keys even on failures."""
        from repo_context import get_repo_context
        # Patch DB to return no cached row and accept insert
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None  # no cache hit
        mock_conn.cursor.return_value = mock_cur

        contributing_body = b'# Contributing\n\nPlease open an issue first.'
        pr_template_body = b'## Description\n\nExplain your change.'
        prs_body = json.dumps([
            {'number': 1, 'title': 'Fix bug', 'body': 'Fixes #123',
             'user': {'type': 'User'}, 'author_association': 'CONTRIBUTOR'}
        ]).encode()
        pr_diff_body = b'diff --git a/foo.py b/foo.py\n+def foo(): pass'

        responses = {
            'contents/CONTRIBUTING.md': (200, contributing_body),
            '.github/PULL_REQUEST_TEMPLATE': (200, pr_template_body),
            '/pulls?': (200, prs_body),
            '/pulls/1': (200, pr_diff_body),
        }

        with self._mock_http(responses):
            with patch('repo_context.get_connection', return_value=mock_conn):
                result = get_repo_context('owner/repo', 'fake-pat')

        self.assertIn('contributing_md', result)
        self.assertIn('pr_template', result)
        self.assertIn('merged_prs', result)
        self.assertIn('naming_conventions', result)
        self.assertIn('repo_full_name', result)
        self.assertEqual(result['repo_full_name'], 'owner/repo')
        self.assertIn('Contributing', result['contributing_md'])

    def test_cache_hit_skips_fetch(self):
        """Returns cached result without making HTTP calls when cache is fresh."""
        from repo_context import get_repo_context
        import datetime

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # Return a fresh cache row (fetched_at = now)
        mock_cur.fetchone.return_value = (
            'owner/repo',
            'cached contributing',
            'cached template',
            json.dumps([{'title': 'old PR', 'body': 'b', 'diff_excerpt': 'd'}]),
            'cached conventions',
            datetime.datetime.now(datetime.timezone.utc),
        )
        mock_conn.cursor.return_value = mock_cur

        with patch('repo_context.get_connection', return_value=mock_conn):
            with patch('urllib.request.urlopen') as mock_http:
                result = get_repo_context('owner/repo', 'fake-pat')
                mock_http.assert_not_called()

        self.assertEqual(result['contributing_md'], 'cached contributing')

    def test_graceful_on_404(self):
        """Returns empty strings for missing files, does not raise."""
        import urllib.error
        from repo_context import get_repo_context

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cur

        def raise_404(req, timeout=None):
            raise urllib.error.HTTPError(None, 404, 'Not Found', {}, None)

        with patch('urllib.request.urlopen', side_effect=raise_404):
            with patch('repo_context.get_connection', return_value=mock_conn):
                result = get_repo_context('owner/repo', 'fake-pat')

        self.assertEqual(result['contributing_md'], '')
        self.assertEqual(result['pr_template'], '')
        self.assertEqual(result['merged_prs'], [])


if __name__ == '__main__':
    unittest.main()
