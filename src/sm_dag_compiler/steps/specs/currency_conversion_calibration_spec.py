"""
Currency Conversion Calibration Step Specification.

This module defines the declarative specification for currency conversion steps
specifically for calibration data, including their dependencies and outputs.
"""

from ...core.base.specification_base import StepSpecification, DependencySpec, OutputSpec, DependencyType, NodeType
from ..registry.step_names import get_spec_step_type

# Import the contract at runtime to avoid circular imports
def _get_currency_conversion_contract():
    from ..contracts.currency_conversion_contract import CURRENCY_CONVERSION_CONTRACT
    return CURRENCY_CONVERSION_CONTRACT

# Currency Conversion Calibration Step Specification
CURRENCY_CONVERSION_CALIBRATION_SPEC = StepSpecification(
    step_type=get_spec_step_type("CurrencyConversion") + "_Calibration",
    node_type=NodeType.INTERNAL,
    script_contract=_get_currency_conversion_contract(),
    dependencies=[
        DependencySpec(
            logical_name="data_input",
            dependency_type=DependencyType.PROCESSING_OUTPUT,
            required=True,
            compatible_sources=["TabularPreprocessing", "ProcessingStep", "CradleDataLoading"],
            semantic_keywords=["calibration", "data", "processed", "tabular", "currency", "monetary", "conversion"],
            data_type="S3Uri",
            description="Processed calibration data requiring currency conversion"
        )
    ],
    outputs=[
        OutputSpec(
            logical_name="converted_data",
            output_type=DependencyType.PROCESSING_OUTPUT,
            property_path="properties.ProcessingOutputConfig.Outputs['converted_data'].S3Output.S3Uri",
            data_type="S3Uri",
            description="Currency-converted calibration data with standardized monetary values"
        )
    ]
)
