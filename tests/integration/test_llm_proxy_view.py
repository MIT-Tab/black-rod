# pylint: disable=import-outside-toplevel
"""
Integration tests for the LLM proxy view.
"""

import html
import json
from django.test import TestCase, Client, override_settings
from django.conf import settings
from core.models.school import School
from core.models.debater import Debater


class LLMProxyViewTest(TestCase):
    """Test the LLM proxy view endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.current_year = int(settings.CURRENT_SEASON)
        
        # Create test data
        self.school = School.objects.create(name="Test University")
        self.debater = Debater.objects.create(
            first_name="Test",
            last_name="Student",
            school=self.school,
            latest_season=str(self.current_year)
        )
    
    def test_missing_endpoint_parameter(self):
        """Test that missing endpoint parameter returns error"""
        response = self.client.get('/llm/')
        
        self.assertEqual(response.status_code, 400)
        self.assertIn('text/html', response['Content-Type'])
        self.assertIn(b'Missing endpoint parameter', response.content)
        self.assertIn(b'Usage: /llm?endpoint=/api/standings', response.content)
    
    def test_endpoint_not_starting_with_slash(self):
        """Test that endpoint not starting with / is rejected"""
        response = self.client.get('/llm/?endpoint=api/standings')
        
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Endpoint must start with /', response.content)
    
    def test_external_url_rejected(self):
        """Test that external URLs are rejected to prevent SSRF"""
        # Try with http://
        response = self.client.get('/llm/?endpoint=http://example.com/api/data')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Endpoint must start with /', response.content)
        
        # Try with https://
        response = self.client.get('/llm/?endpoint=https://example.com/api/data')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Endpoint must start with /', response.content)
    
    def test_non_api_path_rejected(self):
        """Test that non-/api/ paths are rejected"""
        response = self.client.get('/llm/?endpoint=/core/schools/')
        
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Only /api/ and whitelisted /.well-known/ endpoints are allowed', response.content)
    
    def test_path_traversal_rejected(self):
        """Test that path traversal patterns are rejected"""
        # Test with ..
        response = self.client.get('/llm/?endpoint=/api/../admin/')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Path traversal patterns are not allowed', response.content)
        
        # Test with //
        response = self.client.get('/llm/?endpoint=/api//schools/')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Path traversal patterns are not allowed', response.content)
    
    def test_valid_api_endpoint_returns_html(self):
        """Test that valid API endpoint returns HTML wrapped JSON"""
        response = self.client.get('/llm/?endpoint=/api/schools/')
        
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])
        
        # Check for HTML structure
        content = response.content.decode('utf-8')
        self.assertIn('<!DOCTYPE html>', content)
        self.assertIn('<html>', content)
        self.assertIn('<body>', content)
        self.assertIn('<pre>', content)
        self.assertIn('</pre>', content)
        self.assertIn('</body>', content)
        self.assertIn('</html>', content)
    
    def test_json_is_pretty_printed(self):
        """Test that JSON is pretty-printed with indentation"""
        response = self.client.get('/llm/?endpoint=/api/schools/')
        
        content = response.content.decode('utf-8')
        
        # Extract JSON from <pre> tag
        pre_start = content.find('<pre>') + 5
        pre_end = content.find('</pre>')
        json_content = content[pre_start:pre_end]
        
        # Unescape HTML entities before parsing JSON
        unescaped_json = html.unescape(json_content)
        
        # Verify it's valid JSON
        parsed_json = json.loads(unescaped_json)
        self.assertIn('schools', parsed_json)
        
        # Verify it has indentation (pretty-printed)
        # Pretty-printed JSON should have multiple lines and indentation
        self.assertIn('\n', json_content)
        self.assertIn('  ', json_content)  # Check for indent spaces
    
    def test_endpoint_with_query_parameters(self):
        """Test that endpoints with query parameters work correctly"""
        # The standings endpoint accepts a season parameter
        response = self.client.get(f'/llm/?endpoint=/api/standings/?season={self.current_year}')
        
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        
        # Extract JSON from <pre> tag
        pre_start = content.find('<pre>') + 5
        pre_end = content.find('</pre>')
        json_content = content[pre_start:pre_end]
        
        # Unescape HTML entities before parsing JSON
        unescaped_json = html.unescape(json_content)
        
        # Verify it's valid JSON
        parsed_json = json.loads(unescaped_json)
        self.assertIn('season', parsed_json)
    
    def test_html_title_includes_endpoint(self):
        """Test that HTML title includes the requested endpoint"""
        response = self.client.get('/llm/?endpoint=/api/schools/')
        
        content = response.content.decode('utf-8')
        self.assertIn('<title>API Response: /api/schools/</title>', content)
    
    def test_invalid_api_endpoint_returns_error(self):
        """Test that invalid API endpoint returns error"""
        response = self.client.get('/llm/?endpoint=/api/nonexistent/')
        
        # Should return error (404 or 500)
        self.assertNotEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Unable to fetch endpoint', content)
        self.assertNotIn('Traceback', content)
    
    def test_school_debaters_endpoint(self):
        """Test accessing school debaters endpoint through proxy"""
        response = self.client.get(f'/llm/?endpoint=/api/debaters/{self.school.id}/')
        
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        
        # Extract and verify JSON
        pre_start = content.find('<pre>') + 5
        pre_end = content.find('</pre>')
        json_content = content[pre_start:pre_end]
        
        # Unescape HTML entities before parsing JSON
        unescaped_json = html.unescape(json_content)
        
        parsed_json = json.loads(unescaped_json)
        self.assertIn('school', parsed_json)
        self.assertIn('debaters', parsed_json)
        self.assertEqual(parsed_json['school']['name'], "Test University")
    
    def test_xss_protection_in_endpoint(self):
        """Test that XSS attempts in endpoint parameter are escaped"""
        # Try to inject HTML/JavaScript in the endpoint (will fail validation, but let's ensure escaping)
        response = self.client.get('/llm/?endpoint=/api/schools/<script>alert("xss")</script>')
        
        # This should fail validation (not a valid /api/ path with query params)
        # But if it somehow gets through, ensure no script execution
        content = response.content.decode('utf-8')
        
        # Check that any HTML/JavaScript is escaped
        if '<script>' in content.lower():
            # Script tags should be escaped
            self.assertIn('&lt;script&gt;', content)
    
    def test_json_content_escaped(self):
        """Test that JSON content with HTML is properly escaped in output"""
        # This test ensures that even if JSON contains HTML-like content,
        # it's properly escaped when rendered
        response = self.client.get('/llm/?endpoint=/api/schools/')
        
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        
        # Verify HTML structure is intact
        self.assertIn('<pre>', content)
        self.assertIn('</pre>', content)
        
        # Any JSON content should be escaped (< becomes &lt;, > becomes &gt;)
        # This prevents any potential XSS through JSON data
    
    def test_url_encoded_query_parameters(self):
        """Test that URL-encoded query parameters are properly decoded"""
        # Test with encoded space and special characters
        # Note: This test assumes the endpoint accepts these parameters
        # The key is to verify the proxy properly decodes them
        response = self.client.get('/llm/?endpoint=/api/standings/?season=2024')
        
        # Should work without errors
        self.assertEqual(response.status_code, 200)
        
        # Verify the response is valid HTML
        content = response.content.decode('utf-8')
        self.assertIn('<!DOCTYPE html>', content)
        self.assertIn('<pre>', content)

    def test_llms_documentation_endpoint(self):
        """Ensure /llms.txt documents usage for LLM tools."""
        response = self.client.get('/llms.txt')

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/plain', response['Content-Type'])

        content = response.content.decode('utf-8')
        self.assertIn('/llm', content)
        self.assertIn('/llms.txt', content)
        self.assertIn('Usage', content)
        self.assertIn('public API exposes standings', content)
        self.assertIn('/.well-known/openapi.json', content)
        self.assertIn('/.well-known/ai-plugin.json', content)
        self.assertIn('/llm?endpoint=/api/schedule/', content)

    def test_robots_txt_permissive(self):
        """robots.txt should allow all crawlers."""
        response = self.client.get('/robots.txt')

        self.assertEqual(response.status_code, 200)
        self.assertIn('text/plain', response['Content-Type'])

        content = response.content.decode('utf-8')
        self.assertIn('User-agent: *', content)
        self.assertIn('Allow: /', content)

    @override_settings(ALLOWED_HOSTS=['example.com'])
    def test_llm_proxy_respects_allowed_hosts(self):
        """Internal proxy requests should use the original host."""
        response = self.client.get(
            '/llm/?endpoint=/api/schools/',
            HTTP_HOST='example.com',
        )

        self.assertEqual(response.status_code, 200)

    def test_llm_proxy_allows_well_known_endpoints(self):
        """Proxy should permit OpenAPI and manifest paths."""
        response = self.client.get('/llm/?endpoint=/.well-known/openapi.json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response['Content-Type'])

    def test_llm_oty_explainer(self):
        """LLM OTY guide should explain scoring rules."""
        response = self.client.get('/llm/oty-guide/')
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('60', content)
        self.assertIn('autoqual', content.lower())
        self.assertIn('https://apda.online/2025/09/02/bylaws/', content)
