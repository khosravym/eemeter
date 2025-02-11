#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""

   Copyright 2014-2024 OpenEEmeter contributors

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

"""
from typing import Optional, Union

import numpy as np
import pandas as pd
from pandas.tseries.offsets import MonthBegin, MonthEnd

from eemeter.eemeter.common.data_processor_utilities import (
    as_freq,
    clean_billing_daily_data,
    compute_minimum_granularity,
    sufficiency_criteria_baseline,
)
from eemeter.eemeter.common.features import compute_temperature_features
from eemeter.eemeter.common.warnings import EEMeterWarning
from eemeter.eemeter.models.daily.data import _DailyData

"""TODO there is still a ton of unecessarily duplicated code between billing+daily.
    we should be able to perform a few transforms within the billing baseclass, and then call super() for the rest

    unsure whether we should inherit from the public classes because we'll have to take care to use type(data)
    instead of isinstance(data,  _) when doing the checks in the model/wrapper to avoid unintentionally allowing a mix of data/model type
"""


class _BillingData(_DailyData):
    """Baseline data processor for billing data.

    2.2.3.4. Off-cycle reads (spanning less than 25 days) should be dropped from analysis.
    These readings typically occur due to meter reading problems or changes in occupancy.

    2.2.3.5. For pseudo-monthly billing cycles, periods spanning more than 35 days should be dropped from analysis.
    For bi-monthly billing cycles, periods spanning more than 70 days should be dropped from the analysis.
    """

    def _compute_meter_value_df(self, df: pd.DataFrame):
        """
        Computes the meter value DataFrame by cleaning and processing the observed meter data.
        1. The minimum granularity is computed from the non null rows. If the billing cycle is mixed between monthly and bimonthly, then the minimum granularity is bimonthly
        2. The meter data is cleaned and downsampled/upsampled into the correct frequency using clean_billing_daily_data()
        3. Add missing days as NaN by merging with a full year daily index.

        Parameters
        ----------

            df (pd.DataFrame): The DataFrame containing the observed meter data.

        Returns
        -------
            pd.DataFrame: The cleaned and processed meter value DataFrame.
        """
        end = df["observed"].index.max()
        final_value = df["observed"][end]
        final_nan = np.isnan(final_value) if final_value else False
        meter_series = df["observed"].dropna()

        if meter_series.empty:
            return df["observed"].resample("D").first().to_frame()
        if final_nan:
            # preserve end of final billing period
            meter_series[end] = np.nan
        min_granularity = compute_minimum_granularity(
            meter_series.index, default_granularity="billing_bimonthly"
        )

        if min_granularity.startswith("billing") and not final_nan:
            self.warnings.append(
                EEMeterWarning(
                    qualified_name="eemeter.sufficiency_criteria.billing_final_row",
                    description=(
                        "The provided billing data included a real-valued final row. This row was only used"
                        " to denote the end of the final billing period, and can be set to NaN in the future."
                        " If this was the beginning of a billing period that is intended to be measured, append"
                        " one more row corresponding with the ending timestamp of that period."
                    ),
                    data={},
                )
            )

        # Ensure higher frequency data is aggregated to the monthly model
        if not min_granularity.startswith("billing"):
            # MS is so that the date for Month Start
            meter_series = meter_series.resample("MS").sum(min_count=1)
            self.warnings.append(
                EEMeterWarning(
                    qualified_name="eemeter.sufficiency_criteria.inferior_model_usage",
                    description=(
                        "Daily data is provided but the model used is monthly. Are you sure this is the intended model?"
                    ),
                    data={},
                )
            )
            min_granularity = "billing_monthly"

        # This checks for offcycle reads. That is a disqualification if the billing cycle is less than 25 days
        meter_value_df = clean_billing_daily_data(
            meter_series.to_frame("value"), min_granularity, self.disqualification
        )

        # Spread billing data to daily
        meter_value_df = as_freq(meter_value_df["value"], "D").to_frame("value")

        meter_value_df = meter_value_df.rename(columns={"value": "observed"})

        # Drop the NaN at the end
        if len(meter_value_df) > 0 and np.isnan(meter_value_df.iloc[-1]["observed"]):
            # TODO need to refactor the clean caltrack data method and remove the nan logic, this breaks when original df has nan in last row
            meter_value_df = meter_value_df[:-1]

        # Convert all non-zero time datetimes to zero time (i.e., set the time to midnight), for proper join since we only want one reading per day for billing
        meter_value_df.index = meter_value_df.index.normalize()

        # This will ensure that the missing days are kept in the dataframe
        # Create an index with all the days from the start and end date of 'meter_value_df'
        if len(meter_value_df) > 0:
            all_days_index = pd.date_range(
                start=meter_value_df.index.min(),
                end=meter_value_df.index.max(),
                freq="D",
                tz=df.index.tz,
            )
            all_days_df = pd.DataFrame(index=all_days_index)
            meter_value_df = meter_value_df.merge(
                all_days_df, left_index=True, right_index=True, how="outer"
            )

        return meter_value_df

    def _compute_temperature_features(
        self, df: pd.DataFrame, meter_index: pd.DatetimeIndex
    ):
        """
        Compute temperature features for the given DataFrame and meter index.
        1. The frequency of the temperature data is inferred and set to hourly if not already. If frequency is not inferred or its lower than hourly, a warning is added.
        2. The temperature data is downsampled/upsampled into the daily frequency using as_freq()
        3. High frequency temperature data is checked for missing values and a warning is added if more than 50% of the data is missing, and those rows are set to NaN.
        4. If frequency was already hourly, compute_temperature_features() is used to recompute the temperature to match with the meter index.

        Parameters
        ----------

            df (pd.DataFrame): The DataFrame containing temperature data.
            meter_index (pd.DatetimeIndex): The meter index.

        Returns
        -------

            pd.Series: The computed temperature values.
            pd.DataFrame: The computed temperature features.
        """
        temp_series = df["temperature"]
        temp_series.index.freq = temp_series.index.inferred_freq
        if temp_series.index.freq != "H":
            if (
                temp_series.index.freq is None
                or isinstance(temp_series.index.freq, MonthEnd)
                or isinstance(temp_series.index.freq, MonthBegin)
                or temp_series.index.freq > pd.Timedelta(hours=1)
            ):
                # Add warning for frequencies longer than 1 hour
                self.warnings.append(
                    EEMeterWarning(
                        qualified_name="eemeter.sufficiency_criteria.unable_to_confirm_daily_temperature_sufficiency",
                        description=(
                            "Cannot confirm that pre-aggregated temperature data had sufficient hours kept"
                        ),
                        data={},
                    )
                )
            # TODO consider disallowing this until a later patch
            if temp_series.index.freq != "D":
                # Downsample / Upsample the temperature data to daily
                temperature_features = as_freq(
                    temp_series, "D", series_type="instantaneous", include_coverage=True
                )
                # If high frequency data check for 50% data coverage in rollup
                if len(temperature_features[temperature_features.coverage <= 0.5]) > 0:
                    self.warnings.append(
                        EEMeterWarning(
                            qualified_name="eemeter.sufficiency_criteria.missing_high_frequency_temperature_data",
                            description=(
                                "More than 50% of the high frequency Temperature data is missing."
                            ),
                            data={
                                "high_frequency_data_missing_count": len(
                                    temperature_features[
                                        temperature_features.coverage <= 0.5
                                    ].index.to_list()
                                )
                            },
                        )
                    )

                # Set missing high frequency data to NaN
                temperature_features.value[temperature_features.coverage > 0.5] = (
                    temperature_features[temperature_features.coverage > 0.5].value
                    / temperature_features[temperature_features.coverage > 0.5].coverage
                )

                temperature_features = (
                    temperature_features[temperature_features.coverage > 0.5]
                    .reindex(temperature_features.index)[["value"]]
                    .rename(columns={"value": "temperature_mean"})
                )

                if "coverage" in temperature_features.columns:
                    temperature_features = temperature_features.drop(
                        columns=["coverage"]
                    )
            else:
                temperature_features = temp_series.to_frame(name="temperature_mean")

            temperature_features["temperature_null"] = temp_series.isnull().astype(int)
            temperature_features["temperature_not_null"] = temp_series.notnull().astype(
                int
            )
            temperature_features["n_days_kept"] = 0  # unused
            temperature_features["n_days_dropped"] = 0  # unused
        else:
            buffer_idx = pd.to_datetime("2090-01-01 00:00:00+00:00").tz_convert(
                meter_index.tz
            )

            temperature_features = compute_temperature_features(
                meter_index.union([buffer_idx]),
                temp_series,
                data_quality=True,
            )
            temperature_features = temperature_features[:-1]
            # Only check for high frequency temperature data if it exists
            if (
                temperature_features.temperature_not_null
                + temperature_features.temperature_null
            ).median() > 1:
                invalid_temperature_rows = (
                    temperature_features.temperature_not_null
                    / (
                        temperature_features.temperature_not_null
                        + temperature_features.temperature_null
                    )
                ) <= 0.5

                if invalid_temperature_rows.any():
                    self.warnings.append(
                        EEMeterWarning(
                            qualified_name="eemeter.sufficiency_criteria.missing_high_frequency_temperature_data",
                            description=(
                                "More than 50% of the high frequency temperature data is missing."
                            ),
                            data=(invalid_temperature_rows.index.to_list()),
                        )
                    )
                    temperature_features.loc[
                        invalid_temperature_rows, "temperature_mean"
                    ] = np.nan

        temp = temperature_features["temperature_mean"].rename("temperature")
        features = temperature_features.drop(columns=["temperature_mean"])
        return temp, features


class BillingBaselineData(_BillingData):
    """
    Data class to represent Billing Baseline Data. Only baseline data should go into the dataframe input, no blackout data should be input.
    Checks sufficiency for the data provided as input depending on OpenEEMeter specifications and populates disqualifications and warnings based on it.
    Billing data should have an extra month's data appended at the to denote end of period. (Do not append NaN, any other value would work.)

    Parameters
    ----------

    1. data : A dataframe having a datetime index or a datetime column with the timezone also being set.
        It also requires 2 more columns - 'observed' for meter data, and 'temperature' for temperature data.
        The temperature column should have values in Fahrenheit. Please convert your temperatures accordingly.

    2. is_electricity_data : boolean flag to ascertain if this is electricity data or not. Electricity data values of 0 are set to NaN.

    Returns
    -------

    An instance of the BillingBaselineData class.

    Public Attributes
    -----------------

    1. df : Immutable dataframe that contains the meter and temperature values for the baseline data period.
    2. disqualification : Serious issues with the data that can degrade the quality of the model. If you want to go ahead with building the model while ignoring them,
                            set the ignore_disqualification = True flag in the model. By default disqualifications are not ignored.
    3. warnings : Issues with the data, but not that will severely reduce the quality of the model built.

    Public Methods
    --------------

    1. from_series: Public method that can can handle two separate series (meter and temperature) and join them to create a single dataframe.
                    The temperature column should have values in Fahrenheit.

    2. log_warnings: View the disqualifications and warnings associated with the current data input provided.
    """

    def _check_data_sufficiency(self, sufficiency_df):
        """
        Private method which checks the sufficiency of the data for billing baseline calculations using the predefined OpenEEMeter sufficiency criteria.

        Args:
            sufficiency_df (pandas.DataFrame): DataFrame containing the data for sufficiency check. Should have features such as -
            temperature_null: number of temperature null periods in each aggregation step
            temperature_not_null: number of temperature non null periods in each aggregation step

        Returns:
            disqualification (List): List of disqualifications
            warnings (list): List of warnings

        """
        _, disqualification, warnings = sufficiency_criteria_baseline(
            sufficiency_df,
            is_reporting_data=False,
            is_electricity_data=self.is_electricity_data,
        )
        return disqualification, warnings


class BillingReportingData(_BillingData):
    """
    Data class to represent Billing Reporting Data. Only reporting data should go into the dataframe input, no blackout data should be input.
    Checks sufficiency for the data provided as input depending on OpenEEMeter specifications and populates disqualifications and warnings based on it.
    Meter data input is optional for the reporting class.

    Parameters
    ----------

    1. data : A dataframe having a datetime index or a datetime column with the timezone also being set.
        It also requires 1 more column - 'temperature' for temperature data. Adding a column for 'observed', i.e. meter data is optional.
        The temperature column should have values in Fahrenheit. Please convert your temperatures accordingly.

    2. is_electricity_data : boolean flag to ascertain if this is electricity data or not. Electricity data values of 0 are set to NaN.

    Returns
    -------

    An instance of the DailyBaselineData class.

    Public Attributes
    -----------------

    1. df : Immutable dataframe that contains the meter and temperature values for the baseline data period.
    2. disqualification : Serious issues with the data that can degrade the quality of the model. If you want to go ahead with building the model while ignoring them,
                            set the ignore_disqualification = True flag in the model. By default disqualifications are not ignored.
    3. warnings : Issues with the data, but not that will severely reduce the quality of the model built.

    Public Methods
    --------------

    1. from_series: Public method that can can handle two separate series (meter and temperature) and join them to create a single dataframe.
                    The temperature column should have values in Fahrenheit.

    2. log_warnings: View the disqualifications and warnings associated with the current data input provided.
    """

    def __init__(self, df: pd.DataFrame, is_electricity_data: bool):
        df = df.copy()
        if "observed" not in df.columns:
            df["observed"] = np.nan

        super().__init__(df, is_electricity_data)

        # Caltrack 3.5.1.1
        if "observed" in self._df.columns and not self._df.observed.dropna().empty:
            self._df.loc[self._df["temperature"].isna(), "observed"] = np.nan

    @classmethod
    def from_series(
        cls,
        meter_data: Optional[Union[pd.Series, pd.DataFrame]],
        temperature_data: Union[pd.Series, pd.DataFrame],
        is_electricity_data: Optional[bool] = None,
        tzinfo=None,
    ):
        """
        Create a BillingReportingData instance from meter data and temperature data.

        Parameters
        ----------

        - meter_data: pd.Series or pd.DataFrame (Optional attribute)
            The meter data to be used for the BillingReportingData instance.
        - temperature_data: pd.Series or pd.DataFrame (Required)
            The temperature data to be used for the BillingReportingData instance.
        - is_electricity_data: bool (Optional)
            Flag indicating whether the meter data represents electricity data.
        - tzinfo: tz (optional)
            Timezone information to be used for the meter data.

        Returns
        -------

        - BillingReportingData
            A newly created BillingReportingData instance.
        """
        if tzinfo and meter_data is not None:
            raise ValueError(
                "When passing meter data to BillingReportingData, convert its DatetimeIndex to local timezone first; `tzinfo` param should only be used in the absence of reporting meter data."
            )
        if is_electricity_data is None and meter_data is not None:
            raise ValueError(
                "Must specify is_electricity_data when passing meter data."
            )
        if meter_data is None:
            meter_data = pd.DataFrame(
                {"observed": np.nan}, index=temperature_data.index
            )
            if tzinfo:
                meter_data = meter_data.tz_convert(tzinfo)
        return super().from_series(meter_data, temperature_data, is_electricity_data)

    def _check_data_sufficiency(self, sufficiency_df):
        """
        Private method which checks the sufficiency of the data for billing reporting calculations using the predefined OpenEEMeter sufficiency criteria.

        Parameters
        ----------
        1. sufficiency_df (pandas.DataFrame): DataFrame containing the data for sufficiency check. Should have features such as -
            - temperature_null: number of temperature null periods in each aggregation step
            - temperature_not_null: number of temperature non null periods in each aggregation step

        Returns
        -------
            disqualification (List): List of disqualifications
            warnings (list): List of warnings

        """
        _, disqualification, warnings = sufficiency_criteria_baseline(
            sufficiency_df,
            is_reporting_data=True,
            is_electricity_data=self.is_electricity_data,
        )
        return disqualification, warnings
