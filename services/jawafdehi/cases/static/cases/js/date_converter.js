(function() {
    // Unified date converter class
    class DateConverter {
        constructor() {
            this.initialized = false;
        }

        // Position calendar relative to input field
        positionCalendar(inputField) {
            const calendar = document.querySelector('.ndp-container');
            if (calendar) {
                const rect = inputField.getBoundingClientRect();
                const spaceBelow = window.innerHeight - rect.bottom;

                calendar.style.position = 'fixed';
                calendar.style.top = spaceBelow >= 300 ?
                    (rect.bottom + 2) + 'px' :
                    (rect.top - calendar.offsetHeight - 2) + 'px';
                calendar.style.left = rect.left + 'px';

                // Close on scroll
                const closeOnScroll = () => {
                    if (calendar && calendar.parentNode) calendar.remove();
                    window.removeEventListener('scroll', closeOnScroll, true);
                };
                window.addEventListener('scroll', closeOnScroll, true);
            }
        }

        // Convert AD to BS
        adToBs(adValue) {
            if (!adValue) return '';
            try {
                const adDate = NepaliFunctions.ConvertToDateObject(adValue, 'YYYY-MM-DD');
                const bsDate = NepaliFunctions.AD2BS(adDate);
                return bsDate ? NepaliFunctions.ConvertToDateFormat(bsDate, 'YYYY-MM-DD') : '';
            } catch (e) {
                console.error('Error converting AD to BS:', e);
                return '';
            }
        }

        // Convert BS to AD
        bsToAd(bsValue) {
            if (!bsValue) return '';
            try {
                const bsDate = NepaliFunctions.ConvertToDateObject(bsValue, 'YYYY-MM-DD');
                const adDate = NepaliFunctions.BS2AD(bsDate);
                return adDate ? NepaliFunctions.ConvertToDateFormat(adDate, 'YYYY-MM-DD') : '';
            } catch (e) {
                console.error('Error converting BS to AD:', e);
                return '';
            }
        }

        // Setup date pair (AD and BS fields)
        setupDatePair(adField, bsField, enableBsCalendar = true) {
            if (!adField || !bsField) return;

            // Initialize BS from existing AD value
            if (adField.value) {
                bsField.value = this.adToBs(adField.value);
            }

            // AD → BS sync
            adField.addEventListener('change', () => {
                bsField.value = this.adToBs(adField.value);
            });

            if (enableBsCalendar) {
                // Setup BS calendar picker
                bsField.removeAttribute('readonly');
                bsField.nepaliDatePicker({
                    dateFormat: 'YYYY-MM-DD',
                    language: 'english',
                    mode: 'light',
                    container: 'body',
                    onSelect: (dateObj) => {
                        if (dateObj && dateObj.value) {
                            adField.value = this.bsToAd(dateObj.value);
                        }
                    }
                });

                // Make readonly and prevent typing
                bsField.setAttribute('readonly', 'readonly');
                bsField.addEventListener('keydown', (e) => e.preventDefault());
                bsField.addEventListener('click', () => {
                    setTimeout(() => this.positionCalendar(bsField), 10);
                });
            }
        }

        // Initialize all date fields
        init() {
            if (typeof NepaliFunctions === 'undefined' ||
                typeof HTMLElement.prototype.nepaliDatePicker === 'undefined') {
                if (document.readyState === 'complete') {
                    console.warn('Date converter: Required dependencies (NepaliFunctions) not found');
                    return;
                }
                window.addEventListener('load', () => this.init(), { once: true });
                return;
            }

            // Case start/end dates (with calendar)
            this.setupDatePair(
                document.getElementById('id_case_start_date'),
                document.getElementById('id_start_date_bs'),
                true
            );

            this.setupDatePair(
                document.getElementById('id_case_end_date'),
                document.getElementById('id_end_date_bs'),
                true
            );

            // Timeline dates (also with calendar now - unified approach)
            this.initTimelineDates();

            this.initialized = true;
        }

        // Initialize timeline dates with unified approach
        initTimelineDates() {
            // Handle existing timeline rows
            document.querySelectorAll('.timeline-row').forEach(row => {
                const adField = row.querySelector('.timeline-date-ad');
                const bsField = row.querySelector('.timeline-date-bs');
                this.setupDatePair(adField, bsField, true); // Enable calendar for timeline too
            });

            // Watch for new timeline rows being added
            const observer = new MutationObserver((mutations) => {
                mutations.forEach((mutation) => {
                    mutation.addedNodes.forEach((node) => {
                        if (node.classList && node.classList.contains('timeline-row')) {
                            const adField = node.querySelector('.timeline-date-ad');
                            const bsField = node.querySelector('.timeline-date-bs');
                            this.setupDatePair(adField, bsField, true);
                        }
                    });
                });
            });

            observer.observe(document.body, { childList: true, subtree: true });
        }
    }

    // Initialize when DOM is ready
    const converter = new DateConverter();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => converter.init());
    } else {
        converter.init();
    }
})();
