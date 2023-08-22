import numpy as np
import pytest

from eemeter.caltrack.daily.utilities.adaptive_loss import (
    remove_outliers, adaptive_weights, adaptive_loss_fcn
)

def test_remove_outliers():
    # Test case 1: No outliers
    data = np.array([1, 2, 3, 4, 5])
    data_no_outliers, idx_no_outliers = remove_outliers(data)
    assert np.array_equal(data, data_no_outliers)
    assert np.array_equal(idx_no_outliers, np.arange(len(data)))

    # Test case 2: Outliers present
    data = np.array([1, 2, 3, 4, 5, 100])
    data_no_outliers, idx_no_outliers = remove_outliers(data)
    assert np.array_equal(data_no_outliers, np.array([1, 2, 3, 4, 5]))
    assert np.array_equal(idx_no_outliers, np.arange(len(data)-1))

    # Test case 3: Weights provided
    data = np.array([1.0, 2, 3, 4, 5, 100])
    weights = np.array([1, 1, 1, 1, 1, 0.1])
    data_no_outliers, idx_no_outliers = remove_outliers(data, weights)
    assert np.array_equal(data_no_outliers, np.array([1, 2, 3, 4, 5]))
    assert np.array_equal(idx_no_outliers, np.arange(len(data)-1))

def test_adaptive_loss_fcn():
    # Test case 1: Test with all finite values
    x = np.array([1, 2, 3, 4, 5])
    loss_fcn_val, loss_alpha = adaptive_loss_fcn(x)
    assert np.isfinite(loss_fcn_val)
    assert np.isfinite(loss_alpha)

    # Test case 3: Test with zero values
    x = np.array([0, 0, 0, 0, 0])
    loss_fcn_val, loss_alpha = adaptive_loss_fcn(x)
    assert np.isfinite(loss_fcn_val)
    assert np.isfinite(loss_alpha)

    # Test case 4: Test with negative values
    x = np.array([-1, -2, -3, -4, -5])
    loss_fcn_val, loss_alpha = adaptive_loss_fcn(x)
    assert np.isfinite(loss_fcn_val)
    assert np.isfinite(loss_alpha)

    # Test case 5: Test with large values
    x = np.array([1e10, 2e10, 3e10, 4e10, 5e10])
    loss_fcn_val, loss_alpha = adaptive_loss_fcn(x)
    assert np.isfinite(loss_fcn_val)
    assert np.isfinite(loss_alpha)


def test_adaptive_weights():
     # Test case 1: x has not been standardized
    x = np.array([1, 2, 3, 4, 5])
    weights, C, alpha = adaptive_weights(x)
    assert np.allclose(weights, np.array([1, 1, 1, 0.99993894, 0.99992852]), atol = 1e-3)
    assert np.isclose(C, 4.4478)
    assert np.isclose(alpha, 2.0)

    # Test case 2: x has been standardized
    x = np.array([1, 2, 3, 4, 5])
    mu = np.mean(x)
    sigma = np.std(x)
    x = (x - mu) / sigma
    weights, C, alpha = adaptive_weights(x, alpha=3)
    assert np.allclose(weights, np.array([1, 1, 1, 1.02496275, 1.09644634]), atol = 1e-3)
    assert np.isclose(C, 3.1450695413615257)
    assert np.isclose(alpha, 3.0)

    # Test case 3: x contains non-finite values
    x = np.array([1, 2, np.nan, 4, 5])
    weights, C, alpha = adaptive_weights(x)
    assert np.allclose(weights, np.array([1, 1, 0.99993282, 0.99994308, 0.99993282]), atol = 1e-3)
    assert np.isclose(C, 5.55975)
    assert np.isclose(alpha, 2.0)

    # Test case 4: x contains outliers
    x = np.array([1, 2, 3, 4, 5, 100])
    weights, C, alpha = adaptive_weights(x)
    assert np.allclose(weights, np.array([1, 1, 1, 0.98653478, 0.94788002, 0.00106558]), atol = 1e-3)
    assert np.isclose(C, 6.05975)
    assert np.isclose(alpha, -1.09, atol=1e-2)

