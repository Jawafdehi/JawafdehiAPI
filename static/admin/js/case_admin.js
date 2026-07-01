/**
 * Custom JavaScript for Case Admin
 * Handles role-based state field restrictions for Contributors
 */

(function() {
    'use strict';

    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initCaseAdmin);
    } else {
        initCaseAdmin();
    }

    function initCaseAdmin() {
        // Check if we're on a case change/add page
        const stateField = document.querySelector('.contributor-state-field');
        if (!stateField) {
            return;
        }

        // Disable PUBLISHED and CLOSED radio buttons for contributors
        const stateRadios = document.querySelectorAll('input[name="state"]');
        stateRadios.forEach(function(radio) {
            if (radio.value === 'PUBLISHED' || radio.value === 'CLOSED') {
                radio.disabled = true;

                // Add visual styling to disabled options
                const label = radio.closest('label');
                if (label) {
                    label.style.opacity = '0.5';
                    label.style.cursor = 'not-allowed';
                    label.title = 'Only Moderators and Admins can set this state';
                }
            }
        });
    }
})();
