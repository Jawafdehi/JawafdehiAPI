# Widget Static Files

This directory contains the refactored CSS and JavaScript for Django form widgets.

## Files

- `css/widgets.css` - Styles for multi-input widgets
- `js/widgets.js` - JavaScript functionality for dynamic add/remove, drag-and-drop, and auto-resize

## Usage

These files are automatically included via Django's Media class in the widget definitions.

No manual inclusion needed - Django admin will load them automatically.
