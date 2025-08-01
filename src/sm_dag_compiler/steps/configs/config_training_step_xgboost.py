"""
XGBoost Training Step Configuration with Self-Contained Derivation Logic

This module implements the configuration class for SageMaker XGBoost Training steps
using a self-contained design where derived fields are private with read-only properties.
Fields are organized into three tiers:
1. Tier 1: Essential User Inputs - fields that users must explicitly provide
2. Tier 2: System Inputs with Defaults - fields with reasonable defaults that can be overridden
3. Tier 3: Derived Fields - fields calculated from other fields (private with properties)
"""

from pydantic import BaseModel, Field, model_validator, field_validator, PrivateAttr
from typing import List, Optional, Dict, Any, ClassVar
from pathlib import Path
import json
from datetime import datetime

from ..hyperparams.hyperparameters_xgboost import XGBoostModelHyperparameters
from ...core.base.config_base import BasePipelineConfig


class XGBoostTrainingConfig(BasePipelineConfig):
    """
    Configuration specific to the SageMaker XGBoost Training Step.
    This version is streamlined to work with specification-driven architecture.
    Input/output paths are now provided via step specifications and dependencies.
    
    Fields are organized into three tiers:
    1. Tier 1: Essential User Inputs - fields that users must explicitly provide
    2. Tier 2: System Inputs with Defaults - fields with reasonable defaults that can be overridden
    3. Tier 3: Derived Fields - fields calculated from other fields (private with properties)
    """
    # ===== Essential User Inputs (Tier 1) =====
    # These are fields that users must explicitly provide
    
    training_entry_point: str = Field(
        description="Entry point script for XGBoost training.")
    
    # XGBoost specific hyperparameters object
    hyperparameters: XGBoostModelHyperparameters = Field(
        description="Hyperparameters specific to XGBoost model training.")
    
    # ===== System Inputs with Defaults (Tier 2) =====
    # These are fields with reasonable defaults that users can override
    
    # Instance configuration
    training_instance_type: str = Field(
        default='ml.m5.4xlarge', 
        description="Instance type for XGBoost training job.")
    
    training_instance_count: int = Field(
        default=1, 
        ge=1, 
        description="Number of instances for XGBoost training job.")
    
    training_volume_size: int = Field(
        default=30, 
        ge=1, 
        description="Volume size (GB) for training instances.")

    # Framework versions for SageMaker XGBoost container
    framework_version: str = Field(
        default="1.7-1", 
        description="SageMaker XGBoost framework version.")
    
    py_version: str = Field(
        default="py3", 
        description="Python version for the SageMaker XGBoost container.")
    
    # ===== Derived Fields (Tier 3) =====
    # These are fields calculated from other fields, stored in private attributes
    # with public read-only properties for access
    
    _hyperparameter_file: Optional[str] = PrivateAttr(default=None)
    
    class Config(BasePipelineConfig.Config):
        pass

    # Public read-only properties for derived fields
    
    @property
    def hyperparameter_file(self) -> str:
        """Get hyperparameter file path."""
        if self._hyperparameter_file is None:
            self._hyperparameter_file = f"{self.pipeline_s3_loc}/hyperparameters/{self.region}_hyperparameters.json"
        return self._hyperparameter_file

    # Custom model_dump method to include derived properties
    def model_dump(self, **kwargs) -> Dict[str, Any]:
        """Override model_dump to include derived properties."""
        data = super().model_dump(**kwargs)
        # Add derived properties to output
        data["hyperparameter_file"] = self.hyperparameter_file
        return data

    # Initialize derived fields at creation time to avoid potential validation loops
    @model_validator(mode='after')
    def initialize_derived_fields(self) -> 'XGBoostTrainingConfig':
        """Initialize all derived fields once after validation."""
        # Call parent validator first
        super().initialize_derived_fields()
        
        # Initialize training-specific derived fields
        self._hyperparameter_file = f"{self.pipeline_s3_loc}/hyperparameters/{self.region}_hyperparameters.json"
        
        return self

    @model_validator(mode='after')
    def validate_hyperparameter_fields(self) -> 'XGBoostTrainingConfig':
        """
        Validate field lists from hyperparameters.
        """
        # Validate hyperparameters presence
        if not self.hyperparameters:
            raise ValueError("XGBoost hyperparameters must be provided.")

        # Validate field lists
        all_fields = set(self.hyperparameters.full_field_list)
        
        # Check tab_field_list
        if not set(self.hyperparameters.tab_field_list).issubset(all_fields):
            raise ValueError("All fields in tab_field_list must be in full_field_list (from hyperparameters).")
        
        # Check cat_field_list
        if not set(self.hyperparameters.cat_field_list).issubset(all_fields):
            raise ValueError("All fields in cat_field_list must be in full_field_list (from hyperparameters).")
        
        # Check label_name
        if self.hyperparameters.label_name not in all_fields:
            raise ValueError(
                f"label_name '{self.hyperparameters.label_name}' must be in full_field_list (from hyperparameters)."
            )
        
        # Check id_name
        if self.hyperparameters.id_name not in all_fields:
            raise ValueError(
                f"id_name '{self.hyperparameters.id_name}' must be in full_field_list (from hyperparameters)."
            )

        return self
    
    @field_validator('training_instance_type')
    @classmethod
    def _validate_sagemaker_xgboost_instance_type(cls, v: str) -> str:
        # Common CPU instances for XGBoost. XGBoost can also use GPU instances (e.g., ml.g4dn, ml.g5)
        # if tree_method='gpu_hist' is used and framework supports it.
        valid_cpu_instances = [
            "ml.m5.large", "ml.m5.xlarge", "ml.m5.2xlarge", "ml.m5.4xlarge",
            "ml.m5.12xlarge", "ml.m5.24xlarge",
            "ml.c5.large", "ml.c5.xlarge", "ml.c5.2xlarge", "ml.c5.4xlarge",
            "ml.c5.9xlarge", "ml.c5.18xlarge",
        ]
        valid_gpu_instances = [ # For GPU accelerated XGBoost
            "ml.g4dn.xlarge", "ml.g4dn.2xlarge", "ml.g4dn.4xlarge", 
            "ml.g4dn.8xlarge", "ml.g4dn.12xlarge", "ml.g4dn.16xlarge",
            "ml.g5.xlarge", "ml.g5.2xlarge", "ml.g5.4xlarge",
            "ml.g5.8xlarge", "ml.g5.12xlarge", "ml.g5.16xlarge",
             "ml.p3.2xlarge" # Older but sometimes used
        ]
        valid_instances = valid_cpu_instances + valid_gpu_instances
        if v not in valid_instances:
            raise ValueError(
                f"Invalid training instance type for XGBoost: {v}. "
                f"Must be one of: {', '.join(valid_instances)}"
            )
        return v
    
    def get_public_init_fields(self) -> Dict[str, Any]:
        """
        Override get_public_init_fields to include XGBoost training-specific fields.
        Gets a dictionary of public fields suitable for initializing a child config.
        Includes both base fields (from parent) and XGBoost training-specific fields.
        
        Returns:
            Dict[str, Any]: Dictionary of field names to values for child initialization
        """
        # Get fields from parent class (BasePipelineConfig)
        base_fields = super().get_public_init_fields()
        
        # Add XGBoost training-specific fields (Tier 1 and Tier 2)
        training_fields = {
            'training_entry_point': self.training_entry_point,
            'training_instance_type': self.training_instance_type,
            'training_instance_count': self.training_instance_count,
            'training_volume_size': self.training_volume_size,
            'framework_version': self.framework_version,
            'py_version': self.py_version,
        }
        
        # Hyperparameters require special handling since it's a Pydantic model
        if self.hyperparameters:
            # Get hyperparameters as a dictionary (initialized appropriately)
            training_fields['hyperparameters'] = self.hyperparameters
        
        # Combine base fields and training fields (training fields take precedence if overlap)
        init_fields = {**base_fields, **training_fields}
        
        return init_fields
    
    def to_hyperparameter_dict(self) -> Dict[str, Any]:
        """Convert configuration to hyperparameter dictionary for training."""
        # Get hyperparameters from the model
        hyperparam_dict = self.hyperparameters.serialize_config()
        
        # Add derived properties from hyperparameters object
        # These properties are not included by default in serialize_config
        hyperparam_dict["objective"] = self.hyperparameters.objective
        hyperparam_dict["eval_metric"] = json.dumps(self.hyperparameters.eval_metric)
        
        # Add any training config-specific hyperparameters
        # (currently none, but this allows for future extensions)
        
        return hyperparam_dict
