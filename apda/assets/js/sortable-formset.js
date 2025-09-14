class SortableFormset {
    constructor(container, options = {}) {
        this.container = $(container);
        this.tbody = $('.sortable-formset');
        this.options = { maxForms: 500, formType: null, ajaxUrl: null, ...options };
        this.bindEvents();
        this.updateDisplayOrder();
        this.initializeSortableWhenReady();
    }
    
    initializeSortableWhenReady() {
        if (typeof $.fn.sortable === 'function') {
            this.initializeSortable();
        } else {
            setTimeout(() => this.initializeSortableWhenReady(), 500);
        }
    }
    
    initializeSortable() {
        if (!this.tbody.length) return;
        
        this.tbody.sortable({
            handle: '.drag-handle',
            placeholder: 'ui-sortable-placeholder',
            helper: 'original',
            forcePlaceholderSize: true,
            tolerance: 'pointer',
            start: (event, ui) => ui.placeholder.html('<td colspan="4" class="sortable-placeholder">Drop here</td>'),
            update: () => this.updateDisplayOrder()
        });
    }
    
    bindEvents() {
        $('#add-form-btn').off('click').on('click', () => this.addForm());
        this.tbody.off('click', '.delete-form').on('click', '.delete-form', (e) => {
            e.preventDefault();
            this.deleteForm($(e.target).closest('tr'));
        });
    }
    
    updateDisplayOrder() {
        this.tbody.find('tr.formset-row:visible').each((index, row) => {
            const $row = $(row);
            const currentIndex = $row.data('form-index');
            
            $row.find('.place-display').text(index + 1);
            $row.find('input[name*="ORDER"]').val(index + 1);
            
            if (currentIndex !== index) {
                this.renumberFormFields($row, currentIndex, index);
                $row.data('form-index', index);
            }
        });
    }
    
    renumberFormFields($row, oldIndex, newIndex) {
        const firstInput = $row.find('input, select, textarea').first();
        if (!firstInput.length) return;
        
        const firstFieldName = firstInput.attr('name');
        if (!firstFieldName) return;
        
        const stepPrefix = firstFieldName.split('-')[0];
        const oldPrefix = `${stepPrefix}-${oldIndex}-`;
        const newPrefix = `${stepPrefix}-${newIndex}-`;

        $row.find('input, select, textarea').each(function() {
            const $field = $(this);
            ['name', 'id'].forEach(attr => {
                const oldVal = $field.attr(attr);
                if (oldVal) $field.attr(attr, oldVal.replace(oldPrefix, newPrefix));
            });
        });
        
        $row.find('label').each(function() {
            const oldFor = $(this).attr('for');
            if (oldFor) $(this).attr('for', oldFor.replace(oldPrefix, newPrefix));
        });
        
        $row.find('.delete-form').attr('data-form-index', newIndex);
    }
    
    addForm() {
        const totalForms = parseInt($('input[name$="TOTAL_FORMS"]').val()) || 0;
        const currentCount = this.tbody.find('tr.formset-row:visible').length;
        
        if (currentCount >= this.options.maxForms) return alert('Maximum number of forms reached');
        
        this.options.ajaxUrl ? this.addFormViaAjax(totalForms) : this.addFormViaTemplate(totalForms);
    }
    
    addFormViaAjax(formIndex) {
        $.ajax({
            url: this.options.ajaxUrl,
            method: 'GET',
            data: { form_index: formIndex, form_type: this.options.formType },
            success: (response) => {
                this.tbody.append(response.html);
                this.updateFormManagement(formIndex + 1);
                this.initializeNewFormFields();
                this.updateDisplayOrder();
            },
            error: () => alert('Error adding new form')
        });
    }
    
    addFormViaTemplate(formIndex) {
        const emptyFormHtml = $('.empty-form').html();
        if (!emptyFormHtml) return alert('No empty form template found');
        
        this.tbody.append(emptyFormHtml.replace(/__prefix__/g, formIndex));
        this.updateFormManagement(formIndex + 1);
        this.initializeNewFormFields();
        this.updateDisplayOrder();
    }
    
    updateFormManagement(newCount) {
        $('input[name$="TOTAL_FORMS"]').val(newCount);
    }
    
    initializeNewFormFields() {
        this.tbody.find('tr:last .django-select2').each(function() {
            if (!$(this).hasClass('select2-hidden-accessible')) $(this).djangoSelect2();
        });
    }
    
    deleteForm($row) {
        const currentCount = this.tbody.find('tr.formset-row:visible').length;

        if (currentCount <= 1) {
            const $table = this.tbody.closest('table');
            $table.css('visibility', 'hidden');
            
            this.addForm();
            setTimeout(() => {
                this.performDelete($row, currentCount);
                $table.css('visibility', 'visible');
            }, 10); 
            return;
        }

        this.performDelete($row, currentCount);
    }
    
    performDelete($row, currentCount) {
        const deleteField = $row.find('input[name*="DELETE"]');
        deleteField.length ? (deleteField.prop('checked', true), $row.hide()) : $row.remove();
        
        if (this.options.formType === 'debater') {
            const schoolField = $row.find(`select[name$="-school"]`);
            const isVirtual = schoolField.find('option:selected').attr('data-virtual') === 'true';
            
            if (!isVirtual) {
                const data = {
                    first_name: $row.find(`input[name$="-first_name"]`).val(),
                    last_name: $row.find(`input[name$="-last_name"]`).val(),
                    school: schoolField.val()
                };
                
                if (data.first_name && data.last_name && data.school) {
                    $.post('/core/debaters/check_and_delete', {
                        ...data,
                        csrfmiddlewaretoken: $('[name=csrfmiddlewaretoken]').val()
                    }).done(r => console.log('Delete result:', r))
                      .fail(() => console.log('Delete failed'));
                }
            }
        } else if (this.options.formType === 'school') {
            const nameField = $row.find(`input[name$="-name"]`);
            const data = { name: nameField.val() };
            
            if (data.name && !nameField.closest('form').find('[name*="2-"]').length) {
                $.post('/core/schools/check_and_delete', {
                    ...data,
                    csrfmiddlewaretoken: $('[name=csrfmiddlewaretoken]').val()
                }).done(r => console.log('Delete result:', r))
                  .fail(() => console.log('Delete failed'));
            }
        }
        
        // Only update form management if we're not adding a form (to avoid double counting)
        if (currentCount > 1) {
            this.updateFormManagement(currentCount - 1);
        }
        this.updateDisplayOrder();
    }
}

window.SortableFormset = SortableFormset;