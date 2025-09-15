class SortableFormset {
    constructor(container, options = {}) {
        this.container = $(container);
        this.tbody = $('.sortable-formset');
        this.options = {
            maxForms: 500,
            formType: null,
            ajaxUrl: null,
            hasGhostPoints: false,
            itemName: null,
            ...options,
        };
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
        const currentCount = this.tbody.find('tr.formset-row:visible').length;
        
        if (currentCount >= this.options.maxForms) return alert('Maximum number of forms reached');
        
        this.options.ajaxUrl ? this.addFormViaAjax(currentCount) : this.addFormViaTemplate(currentCount);
    }
    
    addFormViaAjax(formIndex) {
        $.ajax({
            url: this.options.ajaxUrl,
            method: 'GET',
            data: {
                form_index: formIndex,
                form_type: this.options.formType,
                has_ghost_points: this.options.hasGhostPoints ? 1 : 0,
                item_name: this.options.itemName,
            },
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
            requestAnimationFrame(() => {
                this.performDelete($row, currentCount);
                $table.css('visibility', 'visible');
            });
            return;
        }

        this.performDelete($row, currentCount);
    }
    
    performDelete($row, currentCount) {
        const deleteField = $row.find('input[name*="DELETE"]');
        
        if (deleteField.length) {
            deleteField.prop('checked', true);
            $row.hide();
        } else {
            $row.remove();
        }

        this.updateDisplayOrder();
        const visibleCount = this.tbody.find('tr.formset-row:visible').length;
        this.updateFormManagement(visibleCount);
    }
}

window.SortableFormset = SortableFormset;

$(document).ready(function() {
    $('[data-form-type]').each(function() {
        const container = $(this);
        const hasGhostPointsRaw = container.data('has-ghost-points');
        const hasGhostPoints = hasGhostPointsRaw === true || hasGhostPointsRaw === 1 || hasGhostPointsRaw === '1';
        const options = {
            maxForms: parseInt(container.data('max-forms')) || 50,
            formType: container.data('form-type'),
            ajaxUrl: container.data('ajax-url'),
            hasGhostPoints,
            itemName: container.data('item-name') || null,
        };
        new SortableFormset(container, options);
    });
});
