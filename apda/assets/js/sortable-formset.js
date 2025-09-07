
class SortableFormset {
    constructor(container, options = {}) {
        this.container = $(container);
        this.tbody = $('.sortable-formset'); // tbody with class sortable-formset
        this.options = {
            maxForms: 100,
            formType: null, 
            ajaxUrl: null,
            ...options
        };
        
        this.init();
    }
    
    init() {
        this.bindEvents();
        this.updateDisplayOrder();
        this.initializeSortableWhenReady();
    }
    
    initializeSortableWhenReady() {
        if (typeof $.fn.sortable === 'function') {
            this.initializeSortable();
        } else {
            setTimeout(() => this.initializeSortableWhenReady(), 100);
        }
    }
    
    initializeSortable() {
        if (this.tbody.length === 0) {
            return;
        }
        
        // Very minimal sortable configuration to preserve form elements
        this.tbody.sortable({
            handle: '.drag-handle',
            placeholder: 'ui-sortable-placeholder',
            helper: 'original',  // Use original element, don't clone
            forcePlaceholderSize: true,
            tolerance: 'pointer',
            start: function(event, ui) {
                // Add placeholder styling
                ui.placeholder.html('<td colspan="4" class="sortable-placeholder">Drop here</td>');
            },
            update: () => {
                // Only update the visual place numbers and hidden order fields
                this.updateDisplayOrder();
            }
        });
    }
    
    bindEvents() {
        $('#add-form-btn').off('click').on('click', () => this.addForm());
        $('#toggle-drag').off('click').on('click', () => this.toggleDrag());
        
        this.tbody.off('click', '.delete-form').on('click', '.delete-form', (e) => {
            e.preventDefault();
            this.deleteForm($(e.target).closest('tr'));
        });
    }
    
    updateDisplayOrder() {
        this.tbody.find('tr.formset-row:visible').each((index, row) => {
            const $row = $(row);
            const currentIndex = $row.data('form-index');
            const newIndex = index;
            
            // Update visual place number
            $row.find('.place-display').text(newIndex + 1);
            
            // Update ORDER field value
            $row.find('input[name*="ORDER"]').val(newIndex + 1);
            
            // If the form index has changed, renumber all form fields
            if (currentIndex !== newIndex) {
                this.renumberFormFields($row, currentIndex, newIndex);
                $row.data('form-index', newIndex);
            }
        });
    }
    
    renumberFormFields($row, oldIndex, newIndex) {
        // Get the step prefix (e.g., "2" from "2-5-ORDER")
        const firstInput = $row.find('input, select, textarea').first();
        if (!firstInput.length) return;
        
        const firstFieldName = firstInput.attr('name');
        if (!firstFieldName) return;
        
        const stepPrefix = firstFieldName.split('-')[0]; // Extract "2" from "2-5-ORDER"
        
        // Update all form field names and IDs in this row
        $row.find('input, select, textarea').each(function() {
            const $field = $(this);
            
            // Update name attribute
            const oldName = $field.attr('name');
            if (oldName) {
                const newName = oldName.replace(`${stepPrefix}-${oldIndex}-`, `${stepPrefix}-${newIndex}-`);
                $field.attr('name', newName);
            }
            
            // Update id attribute
            const oldId = $field.attr('id');
            if (oldId) {
                const newId = oldId.replace(`${stepPrefix}-${oldIndex}-`, `${stepPrefix}-${newIndex}-`);
                $field.attr('id', newId);
            }
        });
        
        // Update labels' for attributes
        $row.find('label').each(function() {
            const $label = $(this);
            const oldFor = $label.attr('for');
            if (oldFor) {
                const newFor = oldFor.replace(`${stepPrefix}-${oldIndex}-`, `${stepPrefix}-${newIndex}-`);
                $label.attr('for', newFor);
            }
        });
        
        // Update delete button data-form-index
        $row.find('.delete-form').attr('data-form-index', newIndex);
    }
    
    addForm() {
        // Get the current TOTAL_FORMS value from Django's management form
        const totalForms = parseInt($('input[name$="TOTAL_FORMS"]').val()) || 0;
        const currentCount = this.tbody.find('tr.formset-row:visible').length;
        
        if (currentCount >= this.options.maxForms) {
            alert('Maximum number of forms reached');
            return;
        }
        
        if (this.options.ajaxUrl) {
            this.addFormViaAjax(totalForms);  // Use TOTAL_FORMS as the index
        } else {
            this.addFormViaTemplate(totalForms);  // Use TOTAL_FORMS as the index
        }
    }
    
    addFormViaAjax(formIndex) {
        $.ajax({
            url: this.options.ajaxUrl,
            method: 'GET',
            data: {
                'form_index': formIndex,
                'form_type': this.options.formType
            },
            success: (response) => {
                this.tbody.append(response.html);
                this.updateFormManagement(formIndex + 1);
                this.initializeNewFormFields();
                this.updateDisplayOrder();
            },
            error: (xhr, status, error) => {
                alert('Error adding new form');
            }
        });
    }
    
    addFormViaTemplate(formIndex) {
        const emptyFormHtml = $('.empty-form').html();
        if (!emptyFormHtml) {
            alert('No empty form template found');
            return;
        }
        
        const newFormHtml = emptyFormHtml.replace(/__prefix__/g, formIndex);
        this.tbody.append(newFormHtml);
        this.updateFormManagement(formIndex + 1);
        this.initializeNewFormFields();
        this.updateDisplayOrder();
    }
    
    updateFormManagement(newCount) {
        $('input[name$="TOTAL_FORMS"]').val(newCount);
    }
    
    initializeNewFormFields() {
        const newRow = this.tbody.find('tr:last');
        newRow.find('.django-select2').each(function() {
            if (!$(this).hasClass('select2-hidden-accessible')) {
                $(this).djangoSelect2();
            }
        });
    }
    
    deleteForm($row) {
        const currentCount = this.tbody.find('tr.formset-row:visible').length;
        
        if (currentCount <= 1) {
            alert('Cannot delete the last form');
            return;
        }
        
        const deleteField = $row.find('input[name*="DELETE"]');
        if (deleteField.length) {
            deleteField.prop('checked', true);
            $row.hide();
        } else {
            $row.remove();
            this.updateFormManagement(currentCount - 1);
        }
        
        this.updateDisplayOrder();
    }
    
    toggleDrag() {
        const $button = $('#toggle-drag');
        const isEnabled = $button.data('enabled');
        
        if (!this.tbody.hasClass('ui-sortable')) {
            alert('Drag and drop is not yet initialized');
            return;
        }
        
        if (isEnabled) {
            this.tbody.sortable('disable');
            $button.html('<i class="fas fa-arrows-alt"></i> Enable Drag & Drop')
                   .removeClass('btn-info').addClass('btn-secondary')
                   .data('enabled', false);
            $('#formset-container').addClass('drag-disabled');
        } else {
            this.tbody.sortable('enable');
            $button.html('<i class="fas fa-arrows-alt"></i> Disable Drag & Drop')
                   .removeClass('btn-secondary').addClass('btn-info')
                   .data('enabled', true);
            $('#formset-container').removeClass('drag-disabled');
        }
    }
}

window.SortableFormset = SortableFormset;
