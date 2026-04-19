# OpenRouter Model Configuration

## Documentation Links

- **OpenRouter Models Documentation**: https://openrouter.ai/docs/models
- **OpenRouter API Reference**: https://openrouter.ai/docs/api
- **Current Model List**: https://openrouter.ai/api/v1/models (JSON endpoint)

## Current Model Names (April 2026)

Based on OpenRouter documentation and search results from April 2026:

### Anthropic Models
- `anthropic/claude-sonnet-4.6` - Current Sonnet version (released Feb 2026)
- `anthropic/claude-opus-4.6` - Current Opus version (released Feb 2026)
- `anthropic/claude-opus-4.5` - Previous Opus version
- `anthropic/claude-opus-4` - Older Opus version

### OpenAI Models
- `openai/gpt-4o` - GPT-4 Omni
- `openai/gpt-4o-mini` - Smaller, cheaper version
- `openai/gpt-5.2` - Latest GPT model (March 2026)

### Google Models
- `google/gemini-pro-1.5` - Gemini Pro 1.5
- `google/gemini-3.1-pro-preview` - Latest Gemini Pro (Feb 2026)
- `google/gemini-3.1-flash-lite` - Fast, lightweight version

### DeepSeek Models
- `deepseek/deepseek-chat` - DeepSeek Chat
- `deepseek/deepseek-v3.2` - Latest DeepSeek V3.2

### Other Models
- `mistralai/devstral-2-2512` - Devstral 2 (March 2026)
- `openrouter/auto` - Auto-routing model (OpenRouter picks best model)

## How to Verify Model Names

1. **Check OpenRouter website**: Visit https://openrouter.ai/models
2. **Use API endpoint**: `curl https://openrouter.ai/api/v1/models`
3. **Check model documentation**: Each model has a dedicated page at `https://openrouter.ai/{model-slug}`

## Common Issues

1. **Outdated model names**: Model versions update frequently (e.g., `claude-sonnet-4` → `claude-sonnet-4.6`)
2. **Incorrect prefixes**: Must include provider prefix (e.g., `anthropic/`, `openai/`)
3. **Deprecated models**: Some models may be removed or have limited availability

## Best Practices

1. **Check documentation regularly**: Model names can change with new releases
2. **Use current versions**: Newer versions often have better performance and features
3. **Test with cheap models first**: Use `openai/gpt-4o-mini` or similar for testing
4. **Monitor OpenRouter announcements**: Follow https://openrouter.ai/blog for updates

## Example `.env` Configuration

```env
# Strong reasoning models recommended for improver and reviewer
IMPROVER_MODEL=anthropic/claude-sonnet-4.6
REVIEWER_MODEL=anthropic/claude-opus-4.6

# Diverse set of models for researchers
RESEARCHER_MODELS=anthropic/claude-opus-4.6,anthropic/claude-sonnet-4.6,openai/gpt-4o,google/gemini-pro-1.5,deepseek/deepseek-chat
```