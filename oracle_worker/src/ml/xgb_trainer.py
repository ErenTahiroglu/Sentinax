import xgboost as xgb
import polars as pl
import logging
import gc
import numpy as np
from typing import Tuple

logger = logging.getLogger(__name__)

class XgbTrainerMemoryOptimized:
    def __init__(self, params: dict = None):
        self.params = params or {
            "objective": "reg:squarederror",
            "tree_method": "hist", # RULE: Memory efficient histogram method
            "device": "cpu",       # Oracle A1 is ARM CPU
            "learning_rate": 0.05,
            "max_depth": 6,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "nthread": 4           # Oracle A1 has 4 OCPU
        }

    def train(self, X: pl.DataFrame, y: pl.Series) -> xgb.Booster:
        """
        Trains an XGBoost model using QuantileDMatrix for minimal memory footprint.
        """
        logger.info(f"Starting XGBoost training on {X.shape[0]} samples...")
        
        # RULE: Use float32 to save memory
        X_np = X.to_numpy().astype(np.float32)
        y_np = y.to_numpy().astype(np.float32)
        
        # RULE: Use QuantileDMatrix (Significantly lower RAM usage than DMatrix)
        dtrain = xgb.QuantileDMatrix(X_np, label=y_np)
        
        # Clear original numpy arrays to save RAM
        del X_np
        del y_np
        gc.collect()
        
        # Train model
        model = xgb.train(
            self.params,
            dtrain,
            num_boost_round=100
        )
        
        logger.info("XGBoost training complete.")
        
        # Explicitly delete DMatrix and trigger GC
        del dtrain
        gc.collect()
        
        return model

    def predict(self, model: xgb.Booster, X: pl.DataFrame) -> np.ndarray:
        """
        Memory efficient prediction.
        """
        X_np = X.to_numpy().astype(np.float32)
        dtest = xgb.DMatrix(X_np)
        
        preds = model.predict(dtest)
        
        del X_np
        del dtest
        gc.collect()
        
        return preds
