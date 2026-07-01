# Static Files

This directory contains custom static files (CSS, JavaScript, images) for the application.

## Directory Structure

```
static/
├── css/          # Custom stylesheets
├── js/           # Custom JavaScript files
├── images/       # Images and icons
└── README.md     # This file
```

## Usage

### Development
In development (`DEBUG=True`), Django serves static files automatically.

### Production
1. Collect static files: `python manage.py collectstatic`
2. Static files are served by WhiteNoise middleware
3. Files are compressed and cached for performance

## Adding Static Files

1. Place your files in this directory (e.g., `static/css/custom.css`)
2. Reference in templates: `{% load static %}` then `{% static 'css/custom.css' %}`
3. Run `collectstatic` before deploying to production

## Configuration

Static files are configured in `config/settings.py`:
- `STATIC_URL`: URL prefix for static files
- `STATIC_ROOT`: Where collectstatic gathers files
- `STATICFILES_DIRS`: Additional locations to search for static files
- WhiteNoise handles compression and caching
