#!/usr/bin/env python
# -*- coding: utf-8 -*--

# Copyright (c) 2023 Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/

import os
import tempfile
import time
from abc import ABC, abstractmethod
from typing import Tuple
import traceback

import fsspec
import numpy as np
import pandas as pd

from ads.opctl.operator.lowcode.forecast.utils import default_signer
from ads.common.object_storage_details import ObjectStorageDetails
from ads.opctl import logger

from .. import utils
from ..const import SUMMARY_METRICS_HORIZON_LIMIT, SupportedMetrics, SupportedModels, SpeedAccuracyMode
from ..operator_config import ForecastOperatorConfig, ForecastOperatorSpec
from ads.common.decorator.runtime_dependency import runtime_dependency
from .forecast_datasets import ForecastDatasets, ForecastOutput


class ForecastOperatorBaseModel(ABC):
    """The base class for the forecast operator models."""

    def __init__(self, config: ForecastOperatorConfig, datasets: ForecastDatasets):
        """Instantiates the ForecastOperatorBaseModel instance.

        Properties
        ----------
        config: ForecastOperatorConfig
            The forecast operator configuration.
        """
        self.config: ForecastOperatorConfig = config
        self.spec: ForecastOperatorSpec = config.spec
        self.datasets: ForecastDatasets = datasets

        self.original_user_data = datasets.original_user_data
        self.original_total_data = datasets.original_total_data
        self.original_additional_data = datasets.original_additional_data
        self.full_data_dict = datasets.full_data_dict
        self.target_columns = datasets.target_columns
        self.categories = datasets.categories

        self.test_eval_metrics = None
        self.original_target_column = self.spec.target_column

        # these fields are populated in the _build_model() method
        self.models = None
        # "outputs" is a list of outputs generated by the models. These should only be generated when the framework requires the original output for plotting
        self.outputs = None
        self.forecast_output = None

        self.train_metrics = False
        self.forecast_col_name = "yhat"
        self.perform_tuning = self.spec.tuning != None

    def generate_report(self):
        """Generates the forecasting report."""
        import warnings
        from sklearn.exceptions import ConvergenceWarning

        with warnings.catch_warnings():
            warnings.simplefilter(action="ignore", category=FutureWarning)
            warnings.simplefilter(action="ignore", category=UserWarning)
            warnings.simplefilter(action="ignore", category=RuntimeWarning)
            warnings.simplefilter(action="ignore", category=ConvergenceWarning)
            import datapane as dp

            start_time = time.time()
            result_df = self._build_model()
            elapsed_time = time.time() - start_time
            logger.info("Building the models completed in %s seconds", elapsed_time)

            # Generate metrics
            summary_metrics = None
            test_data = None
            self.eval_metrics = None

            if self.spec.generate_report or self.spec.generate_metrics:
                if self.train_metrics:
                    self.eval_metrics = utils.evaluate_train_metrics(
                        self.target_columns,
                        self.datasets,
                        self.forecast_output,
                        self.spec.datetime_column.name,
                        target_col=self.forecast_col_name,
                    )
                else:
                    try:
                        self.eval_metrics = self._generate_train_metrics()
                    except NotImplementedError:
                        logger.warn(
                            f"Training Metrics are not available for model type {self.spec.model}"
                        )

                if self.spec.test_data:
                    try:
                        (
                            self.test_eval_metrics,
                            summary_metrics,
                            test_data,
                        ) = self._test_evaluate_metrics(
                            target_columns=self.target_columns,
                            test_filename=self.spec.test_data.url,
                            output=self.forecast_output,
                            target_col=self.forecast_col_name,
                            elapsed_time=elapsed_time,
                        )
                    except Exception as e:
                        logger.warn("Unable to generate Test Metrics.")
                        logger.debug(f"Full Traceback: {traceback.format_exc()}")
            report_sections = []

            if self.spec.generate_report:
                # build the report
                (
                    model_description,
                    other_sections,
                ) = self._generate_report()

                ds_column_series = self.datasets.get_longest_datetime_column()

                title_text = dp.Text("# Forecast Report")

                md_columns = " * ".join([f"{x} \n" for x in self.target_columns])
                first_10_rows_blocks = [
                    dp.DataTable(
                        df.head(10).rename({col: self.spec.target_column}, axis=1),
                        caption="Start",
                        label=col,
                    )
                    for col, df in self.full_data_dict.items()
                ]

                last_10_rows_blocks = [
                    dp.DataTable(
                        df.tail(10).rename({col: self.spec.target_column}, axis=1),
                        caption="End",
                        label=col,
                    )
                    for col, df in self.full_data_dict.items()
                ]

                data_summary_blocks = [
                    dp.DataTable(
                        df.rename({col: self.spec.target_column}, axis=1).describe(),
                        caption="Summary Statistics",
                        label=col,
                    )
                    for col, df in self.full_data_dict.items()
                ]
                summary = dp.Blocks(
                    dp.Select(
                        blocks=[
                            dp.Group(
                                dp.Text(
                                    f"You selected the **`{self.spec.model}`** model."
                                ),
                                model_description,
                                dp.Text(
                                    "Based on your dataset, you could have also selected "
                                    f"any of the models: `{'`, `'.join(SupportedModels.keys())}`."
                                ),
                                dp.Group(
                                    dp.BigNumber(
                                        heading="Analysis was completed in ",
                                        value=utils.human_time_friendly(elapsed_time),
                                    ),
                                    dp.BigNumber(
                                        heading="Starting time index",
                                        value=ds_column_series.min().strftime(
                                            "%B %d, %Y"
                                        ),
                                    ),
                                    dp.BigNumber(
                                        heading="Ending time index",
                                        value=ds_column_series.max().strftime(
                                            "%B %d, %Y"
                                        ),
                                    ),
                                    dp.BigNumber(
                                        heading="Num series",
                                        value=len(self.target_columns),
                                    ),
                                    columns=4,
                                ),
                                dp.Text("### First 10 Rows of Data"),
                                dp.Select(blocks=first_10_rows_blocks)
                                if len(first_10_rows_blocks) > 1
                                else first_10_rows_blocks[0],
                                dp.Text("----"),
                                dp.Text("### Last 10 Rows of Data"),
                                dp.Select(blocks=last_10_rows_blocks)
                                if len(last_10_rows_blocks) > 1
                                else last_10_rows_blocks[0],
                                dp.Text("### Data Summary Statistics"),
                                dp.Select(blocks=data_summary_blocks)
                                if len(data_summary_blocks) > 1
                                else data_summary_blocks[0],
                                label="Summary",
                            ),
                            dp.Text(
                                "The following report compares a variety of metrics and plots "
                                f"for your target columns: \n {md_columns}.\n",
                                label="Target Columns",
                            ),
                        ]
                    ),
                )

                test_metrics_sections = []
                if (
                    self.test_eval_metrics is not None
                    and not self.test_eval_metrics.empty
                ):
                    sec7_text = dp.Text(f"## Test Data Evaluation Metrics")
                    sec7 = dp.DataTable(self.test_eval_metrics)
                    test_metrics_sections = test_metrics_sections + [sec7_text, sec7]

                if summary_metrics is not None and not summary_metrics.empty:
                    sec8_text = dp.Text(f"## Test Data Summary Metrics")
                    sec8 = dp.DataTable(summary_metrics)
                    test_metrics_sections = test_metrics_sections + [sec8_text, sec8]

                train_metrics_sections = []
                if self.eval_metrics is not None and not self.eval_metrics.empty:
                    sec9_text = dp.Text(f"## Training Data Metrics")
                    sec9 = dp.DataTable(self.eval_metrics)
                    train_metrics_sections = [sec9_text, sec9]

                forecast_text = dp.Text(f"## Forecasted Data Overlaying Historical")
                forecast_sec = utils.get_forecast_plots(
                    self.forecast_output,
                    self.target_columns,
                    horizon=self.spec.horizon,
                    test_data=test_data,
                    ci_interval_width=self.spec.confidence_interval_width,
                )
                forecast_plots = [forecast_text, forecast_sec]

                yaml_appendix_title = dp.Text(f"## Reference: YAML File")
                yaml_appendix = dp.Code(code=self.config.to_yaml(), language="yaml")
                report_sections = (
                    [title_text, summary]
                    + forecast_plots
                    + other_sections
                    + test_metrics_sections
                    + train_metrics_sections
                    + [yaml_appendix_title, yaml_appendix]
                )

            # save the report and result CSV
            self._save_report(
                report_sections=report_sections,
                result_df=result_df,
                metrics_df=self.eval_metrics,
                test_metrics_df=self.test_eval_metrics,
            )

    def _test_evaluate_metrics(
        self, target_columns, test_filename, output, target_col="yhat", elapsed_time=0
    ):
        total_metrics = pd.DataFrame()
        summary_metrics = pd.DataFrame()
        data = None
        try:
            storage_options = (
                default_signer()
                if ObjectStorageDetails.is_oci_path(test_filename)
                else {}
            )
            data = utils._load_data(
                filename=test_filename,
                format=self.spec.test_data.format,
                storage_options=storage_options,
                columns=self.spec.test_data.columns,
            )
        except pd.errors.EmptyDataError:
            logger.warn("Empty testdata file")
            return total_metrics, summary_metrics, None

        if data.empty:
            return total_metrics, summary_metrics, None

        data = self._preprocess(
            data, self.spec.datetime_column.name, self.spec.datetime_column.format
        )
        data, confirm_targ_columns = utils._clean_data(
            data=data,
            target_column=self.original_target_column,
            target_category_columns=self.spec.target_category_columns,
            datetime_column="ds",
        )

        # Calculating Test Metrics
        for cat in self.forecast_output.list_categories():
            target_column_i = self.forecast_output.category_to_target[cat]
            output_forecast_i = self.forecast_output.get_category(cat)
            # Only columns present in test file will be used to generate test error
            if target_column_i in data:
                # Assuming that predictions have all forecast values
                dates = output_forecast_i["Date"]
                # Filling zeros for any date missing in test data to maintain consistency in metric calculation as in all other missing values cases it comes as 0
                y_true = [
                    data.loc[data["ds"] == date, target_column_i].values[0]
                    if date in data["ds"].values
                    else 0
                    for date in dates
                ]
                y_pred_i = output_forecast_i["forecast_value"].values
                y_pred = np.asarray(y_pred_i[-len(y_true) :])

                metrics_df = utils._build_metrics_df(
                    y_true=y_true[-self.spec.horizon :],
                    y_pred=y_pred[-self.spec.horizon :],
                    column_name=target_column_i,
                )
                total_metrics = pd.concat([total_metrics, metrics_df], axis=1)
            else:
                logger.warn(
                    f"Error Generating Metrics: Unable to find {target_column_i} in the test data."
                )

        if total_metrics.empty:
            return total_metrics, summary_metrics, data

        summary_metrics = pd.DataFrame(
            {
                SupportedMetrics.MEAN_SMAPE: np.mean(
                    total_metrics.loc[SupportedMetrics.SMAPE]
                ),
                SupportedMetrics.MEDIAN_SMAPE: np.median(
                    total_metrics.loc[SupportedMetrics.SMAPE]
                ),
                SupportedMetrics.MEAN_MAPE: np.mean(
                    total_metrics.loc[SupportedMetrics.MAPE]
                ),
                SupportedMetrics.MEDIAN_MAPE: np.median(
                    total_metrics.loc[SupportedMetrics.MAPE]
                ),
                SupportedMetrics.MEAN_RMSE: np.mean(
                    total_metrics.loc[SupportedMetrics.RMSE]
                ),
                SupportedMetrics.MEDIAN_RMSE: np.median(
                    total_metrics.loc[SupportedMetrics.RMSE]
                ),
                SupportedMetrics.MEAN_R2: np.mean(
                    total_metrics.loc[SupportedMetrics.R2]
                ),
                SupportedMetrics.MEDIAN_R2: np.median(
                    total_metrics.loc[SupportedMetrics.R2]
                ),
                SupportedMetrics.MEAN_EXPLAINED_VARIANCE: np.mean(
                    total_metrics.loc[SupportedMetrics.EXPLAINED_VARIANCE]
                ),
                SupportedMetrics.MEDIAN_EXPLAINED_VARIANCE: np.median(
                    total_metrics.loc[SupportedMetrics.EXPLAINED_VARIANCE]
                ),
                SupportedMetrics.ELAPSED_TIME: elapsed_time,
            },
            index=["All Targets"],
        )

        """Calculates Mean sMAPE, Median sMAPE, Mean MAPE, Median MAPE, Mean wMAPE, Median wMAPE values for each horizon
        if horizon <= 10."""
        target_columns_in_output = set(target_columns).intersection(data.columns)
        if self.spec.horizon <= SUMMARY_METRICS_HORIZON_LIMIT:
            if set(self.forecast_output.list_target_category_columns()) != set(
                target_columns_in_output
            ):
                logger.warn(
                    f"Column Mismatch between Forecast Output and Target Columns"
                )
            metrics_per_horizon = utils._build_metrics_per_horizon(
                data=data,
                output=self.forecast_output,
                target_columns=target_columns,
                target_col=target_col,
                horizon_periods=self.spec.horizon,
            )
            if not metrics_per_horizon.empty:
                summary_metrics = pd.concat([summary_metrics, metrics_per_horizon])

                new_column_order = [
                    SupportedMetrics.MEAN_SMAPE,
                    SupportedMetrics.MEDIAN_SMAPE,
                    SupportedMetrics.MEAN_MAPE,
                    SupportedMetrics.MEDIAN_MAPE,
                    SupportedMetrics.MEAN_WMAPE,
                    SupportedMetrics.MEDIAN_WMAPE,
                    SupportedMetrics.MEAN_RMSE,
                    SupportedMetrics.MEDIAN_RMSE,
                    SupportedMetrics.MEAN_R2,
                    SupportedMetrics.MEDIAN_R2,
                    SupportedMetrics.MEAN_EXPLAINED_VARIANCE,
                    SupportedMetrics.MEDIAN_EXPLAINED_VARIANCE,
                    SupportedMetrics.ELAPSED_TIME,
                ]
                summary_metrics = summary_metrics[new_column_order]

        return total_metrics, summary_metrics, data

    def _save_report(
        self,
        report_sections: Tuple,
        result_df: pd.DataFrame,
        metrics_df: pd.DataFrame,
        test_metrics_df: pd.DataFrame,
    ):
        """Saves resulting reports to the given folder."""
        import datapane as dp

        if self.spec.output_directory:
            output_dir = self.spec.output_directory.url
        else:
            output_dir = "tmp_fc_operator_result"
            logger.warn(
                "Since the output directory was not specified, the output will be saved to {} directory.".format(
                    output_dir
                )
            )

        if ObjectStorageDetails.is_oci_path(output_dir):
            storage_options = default_signer()
        else:
            storage_options = dict()

        # datapane html report
        if self.spec.generate_report:
            # datapane html report
            with tempfile.TemporaryDirectory() as temp_dir:
                report_local_path = os.path.join(temp_dir, "___report.html")
                utils.block_print()
                dp.save_report(report_sections, report_local_path)
                utils.enable_print()

                report_path = os.path.join(output_dir, self.spec.report_filename)
                with open(report_local_path) as f1:
                    with fsspec.open(
                        report_path,
                        "w",
                        **storage_options,
                    ) as f2:
                        f2.write(f1.read())

        # forecast csv report
        utils._write_data(
            data=result_df,
            filename=os.path.join(output_dir, self.spec.forecast_filename),
            format="csv",
            storage_options=storage_options,
        )

        # metrics csv report
        if self.spec.generate_metrics:
            if metrics_df is not None:
                utils._write_data(
                    data=metrics_df.rename_axis("metrics").reset_index(),
                    filename=os.path.join(output_dir, self.spec.metrics_filename),
                    format="csv",
                    storage_options=storage_options,
                    index=False,
                )
            else:
                logger.warn(
                    f"Attempted to generate the {self.spec.metrics_filename} file with the training metrics, however the training metrics could not be properly generated."
                )

            # test_metrics csv report
            if self.spec.test_data is not None:
                if test_metrics_df is not None:
                    utils._write_data(
                        data=test_metrics_df.rename_axis("metrics").reset_index(),
                        filename=os.path.join(
                            output_dir, self.spec.test_metrics_filename
                        ),
                        format="csv",
                        storage_options=storage_options,
                        index=False,
                    )
                else:
                    logger.warn(
                        f"Attempted to generate the {self.spec.test_metrics_filename} file with the test metrics, however the test metrics could not be properly generated."
                    )
        # explanations csv reports
        if self.spec.generate_explanations:
            try:
                if self.formatted_global_explanation is not None:
                    utils._write_data(
                        data=self.formatted_global_explanation,
                        filename=os.path.join(
                            output_dir, self.spec.global_explanation_filename
                        ),
                        format="csv",
                        storage_options=storage_options,
                        index=True,
                    )
                else:
                    logger.warn(
                        f"Attempted to generate global explanations for the {self.spec.global_explanation_filename} file, but an issue occured in formatting the explanations."
                    )

                if self.formatted_local_explanation is not None:
                    utils._write_data(
                        data=self.formatted_local_explanation,
                        filename=os.path.join(
                            output_dir, self.spec.local_explanation_filename
                        ),
                        format="csv",
                        storage_options=storage_options,
                        index=True,
                    )
                else:
                    logger.warn(
                        f"Attempted to generate local explanations for the {self.spec.local_explanation_filename} file, but an issue occured in formatting the explanations."
                    )
            except AttributeError as e:
                logger.warn(
                    "Unable to generate explanations for this model type or for this dataset."
                )
        logger.info(
            f"The outputs have been successfully "
            f"generated and placed into the directory: {output_dir}."
        )

    def _preprocess(self, data, ds_column, datetime_format):
        """The method that needs to be implemented on the particular model level."""
        data["ds"] = pd.to_datetime(data[ds_column], format=datetime_format)
        if ds_column != "ds":
            data.drop([ds_column], axis=1, inplace=True)
        return data

    @abstractmethod
    def _generate_report(self):
        """
        Generates the report for the particular model.
        The method that needs to be implemented on the particular model level.
        """

    @abstractmethod
    def _build_model(self) -> pd.DataFrame:
        """
        Build the model.
        The method that needs to be implemented on the particular model level.
        """

    def _generate_train_metrics(self) -> pd.DataFrame:
        """
        Generate Training Metrics when fitted data is not available.
        The method that needs to be implemented on the particular model level.
        """
        raise NotImplementedError

    @runtime_dependency(
        module="shap",
        err_msg=(
            "Please run `pip3 install shap` to install the required dependencies for model explanation."
        ),
    )
    def explain_model(self, datetime_col_name, explain_predict_fn) -> dict:
        """
        Generates an explanation for the model by using the SHAP (Shapley Additive exPlanations) library.
        This function calculates the SHAP values for each feature in the dataset and stores the results in the `global_explanation` dictionary.

        Returns
        -------
            dict: A dictionary containing the global explanation for each feature in the dataset.
                    The keys are the feature names and the values are the average absolute SHAP values.
        """
        from shap import PermutationExplainer
        exp_start_time = time.time()
        global_ex_time = 0
        local_ex_time = 0
        logger.info(f"Calculating explanations using {self.spec.explanations_accuracy_mode} mode")
        for series_id in self.target_columns:
            self.series_id = series_id
            self.dataset_cols = (
                self.full_data_dict.get(series_id)
                .set_index(datetime_col_name)
                .drop(series_id, axis=1)
                .columns
            )

            self.bg_data = self.full_data_dict.get(series_id).set_index(
                datetime_col_name
            )
            data = self.bg_data[list(self.dataset_cols)][: -self.spec.horizon][
                list(self.dataset_cols)]
            ratio = SpeedAccuracyMode.ratio[self.spec.explanations_accuracy_mode]
            data_trimmed = data.tail(max(int(len(data) * ratio), 100)).reset_index()
            data_trimmed[datetime_col_name] = data_trimmed[datetime_col_name].apply(lambda x: x.timestamp())
            kernel_explnr = PermutationExplainer(
                model=explain_predict_fn,
                masker=data_trimmed
            )

            kernel_explnr_vals = kernel_explnr.shap_values(data_trimmed)

            if not len(kernel_explnr_vals):
                logger.warn(
                    f"No explanations generated. Ensure that additional data has been provided."
                )
            else:
                self.global_explanation[series_id] = dict(
                    zip(
                        data_trimmed.columns[1:],
                        np.average(np.absolute(kernel_explnr_vals[:, 1:]), axis=0),
                    )
                )
            exp_end_time = time.time()
            global_ex_time = global_ex_time + exp_end_time - exp_start_time

            self.local_explainer(
                kernel_explnr, series_id=series_id, datetime_col_name=datetime_col_name
            )
            local_ex_time = local_ex_time + time.time() - exp_end_time
        logger.info("Global explanations generation completed in %s seconds", global_ex_time)
        logger.info("Local explanations generation completed in %s seconds", local_ex_time)

    def local_explainer(self, kernel_explainer, series_id, datetime_col_name) -> None:
        """
        Generate local explanations using a kernel explainer.

        Parameters
        ----------
            kernel_explainer: The kernel explainer object to use for generating explanations.
        """
        # Get the data for the series ID and select the relevant columns
        # data = self.full_data_dict.get(series_id).set_index(datetime_col_name)
        data_horizon = self.bg_data[-self.spec.horizon:][list(self.dataset_cols)]
        data = data_horizon.reset_index()
        data[datetime_col_name] = data[datetime_col_name].apply(lambda x: x.timestamp())
        # Generate local SHAP values using the kernel explainer
        local_kernel_explnr_vals = kernel_explainer.shap_values(data)

        # Convert the SHAP values into a DataFrame
        local_kernel_explnr_df = pd.DataFrame(
            local_kernel_explnr_vals[:, 1:], columns=data.columns[1:]
        )

        # set the index of the DataFrame to the datetime column
        local_kernel_explnr_df.index = data_horizon.index

        if self.spec.model == SupportedModels.AutoTS:
            local_kernel_explnr_df.drop(
                ["series_id", self.spec.target_column], axis=1, inplace=True
            )

        self.local_explanation[series_id] = local_kernel_explnr_df
