/**
 * Data Entry Form Module
 * Handles formset management, sorting, AJAX operations, and entity tracking
 * for tournament results import
 */

import $ from 'jquery';
import 'jquery-ui/ui/widgets/sortable';

// Module initialization
export function initializeDataEntryForm() {
  $(document).ready(() => {
    // Initialize sortable formsets
    initializeSortableFormsets();
    
    // Add form button handler
    $('.add-form-btn').on('click', function addFormHandler() {
      const tab = $(this).data('tab');
      const table = $(`#${tab}-table`);
      addNewForm(table);
    });
    
    // Delete form button handler
    $(document).on('click', '.delete-form', function deleteFormHandler(e) {
      e.preventDefault();
      const $row = $(this).closest('tr');
      deleteForm($row);
    });
    
    // Form submission handler
    $('#data-entry-form').on('submit', () => {
      updateAllOrderFields();
      // Let the form submit normally
    });
    
  });
}

/**
 * Initialize sortable functionality for all formsets
 */
function initializeSortableFormsets() {
  $('.sortable-formset').each(function initSortable() {
    const $tbody = $(this);
    
    $tbody.sortable({
      handle: '.drag-handle',
      placeholder: 'ui-sortable-placeholder',
      helper: 'original',
      forcePlaceholderSize: true,
      tolerance: 'pointer',
      start(event, ui) {
        ui.placeholder.html('<td colspan="4" class="text-center bg-light">Drop here</td>');
      },
      update() {
        updateDisplayOrder($tbody);
      }
    });
  });
}

/**
 * Update the display order for all visible rows in a formset
 * @param {jQuery} $tbody - The tbody element containing formset rows
 */
function updateDisplayOrder($tbody) {
  $tbody.find('tr.formset-row:visible').each(function updateRow(index) {
    const $row = $(this);
    const currentIndex = $row.data('form-index');
    
    // Update place display
    $row.find('.place-number').text(index + 1);
    
    // Update ORDER field
    $row.find('input[name*="ORDER"]').val(index + 1);
    
    // Renumber form fields if index changed
    if (currentIndex !== index) {
      renumberFormFields($row, currentIndex, index);
      $row.data('form-index', index);
    }
  });
}

/**
 * Renumber all form fields in a row
 * @param {jQuery} $row - The row to renumber
 * @param {number} oldIndex - The old form index
 * @param {number} newIndex - The new form index
 */
function renumberFormFields($row, oldIndex, newIndex) {
  const firstInput = $row.find('input, select, textarea').first();
  if (!firstInput.length) return;
  
  const firstFieldName = firstInput.attr('name');
  if (!firstFieldName) return;
  
  // Extract prefix from field name
  const parts = firstFieldName.split('-');
  if (parts.length < 3) return;
  
  const prefix = parts[0];
  const oldPrefix = `${prefix}-${oldIndex}-`;
  const newPrefix = `${prefix}-${newIndex}-`;
  
  // Update all form fields
  $row.find('input, select, textarea').each(function updateField() {
    const $field = $(this);
    ['name', 'id'].forEach((attr) => {
      const oldVal = $field.attr(attr);
      if (oldVal) {
        $field.attr(attr, oldVal.replace(oldPrefix, newPrefix));
      }
    });
  });
  
  // Update labels
  $row.find('label').each(function updateLabel() {
    const oldFor = $(this).attr('for');
    if (oldFor) {
      $(this).attr('for', oldFor.replace(oldPrefix, newPrefix));
    }
  });
}

/**
 * Add a new form to a formset table via AJAX
 * @param {jQuery} table - The table element to add the form to
 */
function addNewForm(table) {
  const tab = table.data('tab');
  const $tbody = table.find('.sortable-formset');
  const currentCount = $tbody.find('tr.formset-row:visible').length;
  const prefix = table.data('prefix');
  
  // Get AJAX URL and parameters
  const ajaxUrl = table.data('ajax-url');
  const formType = table.data('form-type');
  const hasGhostPoints = table.data('has-ghost-points');
  const itemName = table.data('item-name');
  
  $.ajax({
    url: ajaxUrl,
    method: 'GET',
    data: {
      form_index: currentCount,
      form_type: formType,
      has_ghost_points: hasGhostPoints,
      item_name: itemName
    },
    success(response) {
      // Update the HTML to use the correct prefix
      let html = response.html;
      
      // Replace form prefix in the HTML
      const defaultPrefix = formType === 'team' ? '2' : 
                           formType === 'speaker' ? '3' : 
                           formType === 'school' ? '0' : '1';
      const regex = new RegExp(`${defaultPrefix}-${currentCount}`, 'g');
      html = html.replace(regex, `${prefix}-${currentCount}`);
      
      $tbody.append(html);
      
      // Update management form
      updateManagementForm(prefix, currentCount + 1);
      
      // Initialize select2 on new fields
      initializeNewFormFields($tbody);
      
      // Update display order and counts
      updateDisplayOrder($tbody);
    },
    error() {
      // eslint-disable-next-line no-alert
      alert('Error adding new form');
    }
  });
}

/**
 * Delete a form from a formset
 * @param {jQuery} $row - The row to delete
 */
function deleteForm($row) {
  const $tbody = $row.closest('.sortable-formset');
  const currentCount = $tbody.find('tr.formset-row:visible').length;
  
  if (currentCount <= 1) {
    // Keep at least one form - hide and mark for deletion
    const $table = $tbody.closest('table');
    $table.css('visibility', 'hidden');
    
    performDelete($row);
    addNewForm($table);
    
    setTimeout(() => {
      $table.css('visibility', 'visible');
    }, 100);
  } else {
    performDelete($row);
  }
}

/**
 * Mark a form row for deletion
 * @param {jQuery} $row - The row to delete
 */
function performDelete($row) {
  const $deleteField = $row.find('input[name*="DELETE"]');
  
  if ($deleteField.length) {
    $deleteField.prop('checked', true);
    $row.hide();
  } else {
    $row.remove();
  }
  
  const $tbody = $row.closest('.sortable-formset');
  updateDisplayOrder($tbody);
  
  // Update management form
  const prefix = $row.closest('table').data('prefix');
  const visibleCount = $tbody.find('tr.formset-row:visible').length;
  updateManagementForm(prefix, visibleCount);
}

/**
 * Update the management form with the new count
 * @param {string} prefix - The formset prefix
 * @param {number} newCount - The new form count
 */
function updateManagementForm(prefix, newCount) {
  $(`input[name="${prefix}-TOTAL_FORMS"]`).val(newCount);
}

/**
 * Initialize select2 widgets on newly added forms
 * @param {jQuery} $tbody - The tbody element containing the forms
 */
function initializeNewFormFields($tbody) {
  $tbody.find('tr:last .django-select2').each(function initSelect2() {
    if (!$(this).hasClass('select2-hidden-accessible')) {
      $(this).djangoSelect2();
    }
  });
}

/**
 * Update order fields for all formsets before form submission
 */
function updateAllOrderFields() {
  $('.sortable-formset').each(function updateFormset() {
    updateDisplayOrder($(this));
  });
}

// Auto-initialize when module is loaded
initializeDataEntryForm();
