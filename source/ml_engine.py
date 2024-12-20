from loguru import logger
import pandas as pd
import gc
import pickle
from tqdm import tqdm

from source.utils.session_ml_info import load_or_initialize_results
from source.utils.data_preprocess import scale_forecasters_dataframe, scale_buyer_dataframe, buyer_scaler_statistics, impute_mean_for_nan
from source.utils.data_preprocess import rescale_predictions, rescale_targets, set_non_negative_predictions
from source.utils.quantile_preprocess import extract_quantile_columns, split_quantile_train_test_data, get_numpy_Xy_train_test_quantile
from source.ensemble.stack_generalization.feature_engineering.data_augmentation import create_augmented_dataframe
from source.ensemble.stack_generalization.data_preparation.data_train_test import split_train_test_data, concatenate_feat_targ_dataframes, get_numpy_Xy_train_test
from source.ensemble.stack_generalization.data_preparation.data_train_test import prepare_pre_test_data
from source.ensemble.stack_generalization.ensemble_model import predico_ensemble_predictions_per_quantile, predico_ensemble_variability_predictions
from source.ensemble.stack_generalization.second_stage.create_data_second_stage import create_2stage_dataframe, create_augmented_dataframe_2stage, create_var_ensemble_dataframe, get_numpy_Xy_train_test_2stage
from source.ensemble.stack_generalization.utils.results import collect_quantile_ensemble_predictions, create_ensemble_dataframe, melt_dataframe


def create_ensemble_forecasts(ens_params,
                                df_buyer,
                                df_market,
                                end_training_timestamp,
                                forecast_range,
                                challenge_usecase = None,
                                simulation = False):
    """Create ensemble forecasts for wind power and wind power variability using forecasters predictions
    args:
        ens_params: dict, ensemble parameters
        df_buyer: pd.DataFrame, buyer data
        df_market: pd.DataFrame, market data
        end_training_timestamp: pd.Timestamp, end of training timestamp
        forecast_range: pd.DatetimeIndex, forecast range
        challenge_usecase: str, challenge usecase
        simulation: bool, simulation
    returns:
        results_challenge_dict: dict, results for the challenge
        results_challenge_dict_simulation: dict, results for the challenge simulation"""

    start_prediction_timestamp = forecast_range[0]  # get the start prediction timestamp
    end_prediction_timestamp = forecast_range[-1]  # get the end prediction timestamp

    # Extract quantile columns with checks
    df_ensemble_quantile50 = extract_quantile_columns(df_market, 'q50')  # get the quantile 50 predictions
    # impute mean for NaN values by looping over columns
    if not df_ensemble_quantile50.empty:
        df_ensemble_quantile50 = impute_mean_for_nan(df_ensemble_quantile50) 

    df_ensemble_quantile10 = extract_quantile_columns(df_market, 'q10')  # get the quantile 10 predictions
    if not df_ensemble_quantile10.empty:
        df_ensemble_quantile10 = impute_mean_for_nan(df_ensemble_quantile10)

    df_ensemble_quantile90 = extract_quantile_columns(df_market, 'q90')  # get the quantile 90 predictions
    if not df_ensemble_quantile90.empty:
        df_ensemble_quantile90 = impute_mean_for_nan(df_ensemble_quantile90)

    # Ensure at least one quantile DataFrame is not empty
    if df_ensemble_quantile50.empty:
        raise ValueError("Quantile columns 'q50' were not found in the DataFrame.")
    
    buyer_resource_name = df_buyer.columns[0]  # get the name of the buyer resource
    
    # if the model type is LR, normalization must be True
    if ens_params['model_type'] == 'LR':
        assert ens_params['normalize'] == True or ens_params['standardize'] == True, "Normalize or Standardize must be True for model_type 'LR'"

    # ML ENGINE PREDICO PLATFORM
    logger.info('  ')
    logger.opt(colors=True).info(f'<fg 250,128,114> PREDICO Machine Learning Engine </fg 250,128,114> ')
    logger.info('  ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Launch Time from {str(end_training_timestamp)} </fg 250,128,114> ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Predictions from {str(start_prediction_timestamp)} to {str(end_prediction_timestamp)} </fg 250,128,114> ')
    logger.info('  ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Buyer Resource Name: {buyer_resource_name} </fg 250,128,114>')

    # check rescale_features is true if Normalize is True or Standardize is True
    assert not (ens_params['normalize'] or ens_params['standardize'] and not ens_params['scale_features']), 'scale_features must be True if normalize or standardize is True'

    # check if normalize and standardize are not both True
    assert not (ens_params['normalize'] and ens_params['standardize']), 'normalize and standardize cannot both be True'

    # scale features
    buyer_scaler_stats = buyer_scaler_statistics(ens_params, df_buyer, end_training_timestamp, buyer_resource_name)

    # Logging
    logger.opt(colors=True).info(f'<fg 250,128,114> Collecting forecasters prediction for ensemble learning - model: {ens_params["model_type"]} </fg 250,128,114>')
    logger.info('  ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Forecasters Ensemble DataFrame </fg 250,128,114>')

    # Scale dataframes
    df_ensemble_normalized, df_ensemble_normalized_quantile10, df_ensemble_normalized_quantile90 = scale_forecasters_dataframe(ens_params, buyer_scaler_stats, df_ensemble_quantile50, df_ensemble_quantile10, df_ensemble_quantile90, end_training_timestamp)
    
    # Augment dataframes
    logger.info('   ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Augment DataFrame </fg 250,128,114>')

    df_ensemble_normalized_lag = create_augmented_dataframe(df=df_ensemble_normalized, 
                                                            max_lags=ens_params['max_lags'], 
                                                            forecasters_diversity=ens_params['forecasters_diversity'], 
                                                            add_lags=ens_params['add_lags'], 
                                                            augment_with_poly=ens_params['augment_with_poly'],
                                                            augment_with_roll_stats = ens_params['augment_with_roll_stats'],
                                                            differenciate=ens_params['differenciate'], 
                                                            end_train=end_training_timestamp, 
                                                            start_prediction=start_prediction_timestamp)

    # Augment dataframes quantile predictions
    if ens_params['add_quantile_predictions']:
        logger.opt(colors=True).info(f'<fg 250,128,114> -- Augment quantile predictions </fg 250,128,114>')
        
        if not df_ensemble_normalized_quantile10.empty:
            # Augment with predictions quantile 10
            df_ensemble_normalized_lag_quantile10 = (create_augmented_dataframe(df=df_ensemble_normalized_quantile10,
                                                                                max_lags=ens_params['max_lags'], 
                                                                                forecasters_diversity=ens_params['forecasters_diversity'], 
                                                                                add_lags=ens_params['add_lags'], 
                                                                                augment_with_poly=ens_params['augment_with_poly'],
                                                                                augment_with_roll_stats = ens_params['augment_with_roll_stats'], 
                                                                                differenciate=ens_params['differenciate'], 
                                                                                end_train=end_training_timestamp, 
                                                                                start_prediction=start_prediction_timestamp) \
                                                                                if not df_ensemble_normalized_quantile10.empty else pd.DataFrame())
        else:
            df_ensemble_normalized_lag_quantile10 = pd.DataFrame()

        if not df_ensemble_normalized_quantile90.empty:
            # Augment with predictions quantile 90
            df_ensemble_normalized_lag_quantile90 = (create_augmented_dataframe(df=df_ensemble_normalized_quantile90, 
                                                                                max_lags=ens_params['max_lags'], 
                                                                                forecasters_diversity=ens_params['forecasters_diversity'], 
                                                                                add_lags=ens_params['add_lags'], 
                                                                                augment_with_poly=ens_params['augment_with_poly'],
                                                                                augment_with_roll_stats = ens_params['augment_with_roll_stats'], 
                                                                                differenciate=ens_params['differenciate'], 
                                                                                end_train=end_training_timestamp, 
                                                                                start_prediction=start_prediction_timestamp) \
                                                                                if not df_ensemble_normalized_quantile90.empty else pd.DataFrame())
        else:
            df_ensemble_normalized_lag_quantile90 = pd.DataFrame()
    else:
        df_ensemble_normalized_lag_quantile10, df_ensemble_normalized_lag_quantile90 = pd.DataFrame(), pd.DataFrame()
    
    # Scale buyer dataframe
    df_buyer_norm = scale_buyer_dataframe(ens_params, buyer_scaler_stats, df_buyer)
    
    # # Split train and test dataframes
    df_train_feat, df_test_feat = split_train_test_data(df=df_ensemble_normalized_lag, 
                                                        end_train=end_training_timestamp, 
                                                        start_prediction=start_prediction_timestamp)

    df_train_targ, df_test_targ = split_train_test_data(df=df_buyer_norm, 
                                                        end_train=end_training_timestamp, 
                                                        start_prediction=start_prediction_timestamp)

    df_train_ensemble, df_test_ensemble = concatenate_feat_targ_dataframes(buyer_resource_name=buyer_resource_name, 
                                                                            df_train_ensemble=df_train_feat, df_test_ensemble=df_test_feat, 
                                                                            df_train=df_train_targ, df_test=df_test_targ,  
                                                                            max_lag=ens_params['max_lags'])

    logger.info('   ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Train and Test Dataframes </fg 250,128,114>')
    logger.info(f'Length of Train DataFrame: {len(df_train_ensemble)}')
    logger.info(f'Length of Test DataFrame: {len(df_test_ensemble)}')
    assert len(df_test_ensemble) == 96, 'Test dataframe must have 96 rows'

    # # Split train and test dataframes quantile predictions
    if ens_params['add_quantile_predictions']:
        if not df_ensemble_normalized_quantile10.empty:
            # Quantile 10
            df_train_ensemble_quantile10, df_test_ensemble_quantile10 = split_quantile_train_test_data(
                df_ensemble_normalized_lag_quantile10, end_training_timestamp, start_prediction_timestamp)
        else:
            df_train_ensemble_quantile10 = df_test_ensemble_quantile10 = pd.DataFrame()
        if not df_ensemble_normalized_quantile90.empty:
            # Quantile 90
            df_train_ensemble_quantile90, df_test_ensemble_quantile90 = split_quantile_train_test_data(
                df_ensemble_normalized_lag_quantile90, end_training_timestamp, start_prediction_timestamp)
        else:
            df_train_ensemble_quantile90 = df_test_ensemble_quantile90 = pd.DataFrame()
    else:
        df_train_ensemble_quantile10 = df_test_ensemble_quantile10 = df_train_ensemble_quantile90 = df_test_ensemble_quantile90 = pd.DataFrame()

    
    # Assert df_test matches df_ensemble_test
    assert (df_test_targ.index == df_test_ensemble.index).all(),'Datetime index are not equal'

    # Make X-y train and test sets
    X_train, y_train, X_test, _ = get_numpy_Xy_train_test(df_train_ensemble, df_test_ensemble)

    # Make X-y train and test sets quantile
    X_train_quantile10, X_test_quantile10, X_train_quantile90, X_test_quantile90 = get_numpy_Xy_train_test_quantile(ens_params,
                                                                                                                    df_train_ensemble_quantile10,
                                                                                                                    df_test_ensemble_quantile10,
                                                                                                                    df_train_ensemble_quantile90,
                                                                                                                    df_test_ensemble_quantile90
                                                                                                                    )

    # Assert no NaNs in train ensemble
    assert df_train_ensemble.isna().sum().sum() == 0
    
    # log the number of NaNs in the train and test ensemble
    logger.info('   ')
    logger.info(f'Number of NaNs in the train ensemble: {df_train_ensemble.isna().sum().sum()}')
    logger.info(f'Number of NaNs in the test ensemble: {df_test_ensemble.isna().sum().sum()}')
    
    file_info, iteration, best_results, best_results_var = load_or_initialize_results(ens_params, buyer_resource_name)

    logger.info('   ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Iteration {iteration} </fg 250,128,114>')

    # Run ensemble learning
    logger.info('   ')
    logger.opt(colors=True).info(f'<fg 250,128,114> Compute Ensemble Predictions </fg 250,128,114>')

    # dictioanry to store predictions
    predictions = {}
    previous_day_results_first_stage = {}

    # # for conformalized quantile regression
    # if ens_params['conformalized_qr']:
    #     conformalized_qr = {}

    # Loop over quantiles
    for quantile in tqdm(ens_params['quantiles'], desc='Quantile Regression'):

        # Run ensemble learning
        results_per_quantile_wp = predico_ensemble_predictions_per_quantile(ens_params=ens_params,
                                                                            X_train=X_train, X_test=X_test, y_train=y_train, df_train_ensemble=df_train_ensemble, 
                                                                            predictions=predictions, quantile=quantile, 
                                                                            best_results=best_results, 
                                                                            iteration=iteration, 
                                                                            X_train_quantile10=X_train_quantile10, X_test_quantile10=X_test_quantile10, 
                                                                            df_train_ensemble_quantile10=df_train_ensemble_quantile10, 
                                                                            X_train_quantile90=X_train_quantile90, X_test_quantile90=X_test_quantile90, 
                                                                            df_train_ensemble_quantile90=df_train_ensemble_quantile90)
        
        # Extract results
        predictions = results_per_quantile_wp['predictions']
        best_results = results_per_quantile_wp['best_results'] 
        fitted_model = results_per_quantile_wp['fitted_model'] 
        X_train_augmented = results_per_quantile_wp['X_train_augmented']
        X_test_augmented = results_per_quantile_wp['X_test_augmented']
        df_train_ensemble_augmented = results_per_quantile_wp['df_train_ensemble_augmented']
        if ens_params['model_type'] == 'LR':
            coefs = results_per_quantile_wp['coefs']
            p_values = results_per_quantile_wp['p_values']
            model_summary = results_per_quantile_wp['model-summary'] 

        # if ens_params['conformalized_qr'] and quantile != 0.5:
        #     # for conformalized quantile regression
        #     conformalized_qr[quantile] = {'fitted_model': results_per_quantile_wp['fitted_model'],
        #                                     'X_calibrate_augmented': results_per_quantile_wp['X_calibrate_augmented'],
        #                                     'y_calibrate': results_per_quantile_wp['y_calibrate']}

        # Store results
        previous_day_results_first_stage[quantile] = {"fitted_model" : fitted_model, 
                                                        "X_train_augmented" : X_train_augmented, 
                                                        "X_test_augmented" : X_test_augmented, 
                                                        "df_train_ensemble_augmented" : df_train_ensemble_augmented,
                                                        "buyer_scaler_stats": buyer_scaler_stats
                                                        }
        if ens_params['model_type'] == 'LR':
            previous_day_results_first_stage[quantile].update({"coefs": coefs, "p_values": p_values, "model-summary": model_summary})
        
        # compute variability predictions with as input the predictions of the first stage
        if  quantile == 0.5:
            logger.info('   ')
            logger.opt(colors=True).info(f'<fg 72,201,176> Compute Variability Predictions </fg 72,201,176>')

            ## ------
            
            X_test_augmented, y_test = prepare_pre_test_data(ens_params, quantile, df_test_ensemble, df_test_ensemble_quantile10, df_test_ensemble_quantile90)
            
            predictions_insample = fitted_model.predict(X_train_augmented)
            predictions_outsample = fitted_model.predict(X_test_augmented)

            # if ens_params['conformalized_qr']:
            #     df_train_ensemble = df_train_ensemble.iloc[ens_params['day_calibration']*96:]
            #     y_train = y_train[ens_params['day_calibration']*96:]
            
            # Create 2-stage dataframe
            df_2stage = create_2stage_dataframe(df_train_ensemble, df_test_ensemble, y_train, y_test, predictions_insample, predictions_outsample)
    
            # Augment 2-stage dataframe
            df_2stage_buyer = create_augmented_dataframe_2stage(df_2stage, 
                                                                order_diff = ens_params['order_diff'],
                                                                differentiate=ens_params['differenciate_var'], 
                                                                max_lags=ens_params['max_lags_var'], 
                                                                add_lags = ens_params['add_lags_var'],
                                                                augment_with_poly=ens_params['augment_with_poly_var'],
                                                                end_train=end_training_timestamp,
                                                                start_prediction=start_prediction_timestamp)
                        
            # Split 2-stage dataframe
            df_2stage_train, df_2stage_test = split_train_test_data(df=df_2stage_buyer, 
                                                                    end_train=end_training_timestamp, 
                                                                    start_prediction=start_prediction_timestamp)

            logger.info('   ')
            logger.opt(colors=True).info(f'<fg 72,201,176> Train and Test Dataframes </fg 72,201,176>')
            logger.info(f'Length of Train DataFrame: {len(df_2stage_train)}')
            logger.info(f'Length of Test DataFrame: {len(df_2stage_test)}')
            assert len(df_2stage_test) == 96, 'Test dataframe must have 96 rows'
            
            # Make X-y train and test sets for 2-stage
            X_train_2stage, y_train_2stage, X_test_2stage = get_numpy_Xy_train_test_2stage(df_2stage_train, df_2stage_test)

            # dictioanry to store variability predictions
            variability_predictions = {}
            previous_day_results_second_stage = {}

            variability_predictions_insample = {}
            variability_predictions_outsample = {}

            # Loop over quantiles
            for quantile in tqdm(ens_params['quantiles'], desc='Quantile Regression'):

                # Run ensemble learning
                results_per_quantile_wpv = predico_ensemble_variability_predictions(ens_params = ens_params, 
                                                                                    X_train_2stage=X_train_2stage, 
                                                                                    y_train_2stage=y_train_2stage, 
                                                                                    X_test_2stage=X_test_2stage,
                                                                                    variability_predictions=variability_predictions,
                                                                                    quantile=quantile, 
                                                                                    iteration=iteration, 
                                                                                    best_results_var=best_results_var,
                                                                                    variability_predictions_insample =  variability_predictions_insample,
                                                                                    variability_predictions_outsample = variability_predictions_outsample,)
                
                # Extract results
                variability_predictions = results_per_quantile_wpv['variability_predictions']
                variability_predictions_insample = results_per_quantile_wpv['variability_predictions_insample']
                variability_predictions_outsample = results_per_quantile_wpv['variability_predictions_outsample']
                best_results_var = results_per_quantile_wpv['best_results_var'] 
                var_fitted_model = results_per_quantile_wpv['var_fitted_model'] 
                
                # Store results
                previous_day_results_second_stage[quantile] = {"fitted_model": fitted_model, 
                                                                "var_fitted_model": var_fitted_model, 
                                                                "X_train_augmented": X_train_augmented, 
                                                                "X_test_augmented": X_test_augmented, 
                                                                "df_train_ensemble_augmented": df_train_ensemble_augmented, 
                                                                "df_train_ensemble": df_train_ensemble, 
                                                                "df_test_ensemble": df_test_ensemble,
                                                                "y_train": y_train,
                                                                "buyer_scaler_stats": buyer_scaler_stats
                                                                }

                # Rescale predictions for variability
                variability_predictions = rescale_predictions(variability_predictions, ens_params, buyer_scaler_stats, quantile, stage='2nd')
                variability_predictions_insample = rescale_predictions(variability_predictions_insample, ens_params, buyer_scaler_stats, quantile, stage='2nd')
                variability_predictions_outsample = rescale_predictions(variability_predictions_outsample, ens_params, buyer_scaler_stats, quantile, stage='2nd')

                # Transform predictions to dataframe
                var_pred_insample_df = pd.DataFrame(variability_predictions_insample, index=df_2stage_train.index)
                var_pred_outsample_df = pd.DataFrame(variability_predictions_outsample, index=df_2stage_test.index)


            # Rescale targets for variability
            target_name = 'targets'
            df_2stage_test = rescale_targets(ens_params, buyer_scaler_stats, df_2stage_test, target_name, stage='2nd')

            # Collect quantile variability predictions
            var_predictions_dict = collect_quantile_ensemble_predictions(ens_params['quantiles'], df_2stage_test, variability_predictions)

            # collect results as dataframe
            df_var_ensemble = create_var_ensemble_dataframe(buyer_resource_name, 
                                                            ens_params['quantiles'], 
                                                            var_predictions_dict, 
                                                            df_2stage_test)
            
            # melt dataframe
            df_var_ensemble_melt = melt_dataframe(df_var_ensemble) 

            # delete and collect garbage
            del df_2stage, df_2stage_buyer, df_2stage_train
            gc.collect()

    # if ens_params['conformalized_qr']:
    #     import numpy as np
    #     # get predictions quantile .1 and .9
    #     n = len(conformalized_qr[0.1]['y_calibrate'])
    #     alpha = 0.2
    #     predictions_quantile10 = conformalized_qr[0.1]['fitted_model'].predict(conformalized_qr[0.1]['X_calibrate_augmented'])
    #     predictions_quantile90 = conformalized_qr[0.9]['fitted_model'].predict(conformalized_qr[0.9]['X_calibrate_augmented'])
    #     # get conformal scores
    #     cal_scores = ((conformalized_qr[0.1]['y_calibrate'] - predictions_quantile10) + (predictions_quantile90 - conformalized_qr[0.9]['y_calibrate']))/2
    #     print('length of cal_scores:', len(cal_scores))
    #     # Get the score quantile
    #     qhat = np.quantile(cal_scores, np.ceil((n+1)*(1-alpha))/n, interpolation='higher')
    #     predictions[0.1] = predictions[0.1] - qhat
    #     predictions[0.9] = predictions[0.9] + qhat

    # Loop over quantiles
    for quantile in ens_params['quantiles']:
        # Rescale predictions
        predictions = rescale_predictions(predictions, ens_params, buyer_scaler_stats, quantile, stage='1st') 
        # Ensure predictions are positive
        predictions = set_non_negative_predictions(predictions, quantile) 

        # # delete and collect garbage
        # del X_train_augmented, X_test_augmented, df_train_ensemble_augmented
        # gc.collect()

    # Rescale targets
    target_name = 'norm_' + buyer_resource_name
    df_test_targ = rescale_targets(ens_params, buyer_scaler_stats, df_test_targ, target_name, stage='1st')

    # Collect quantile predictions
    quantile_predictions_dict = collect_quantile_ensemble_predictions(ens_params['quantiles'], df_test_targ, predictions)

    # collect results as dataframe
    df_pred_ensemble = create_ensemble_dataframe(buyer_resource_name,
                                                ens_params['quantiles'],
                                                quantile_predictions_dict, 
                                                df_test_targ)
    
    if simulation:

        assert  challenge_usecase == 'simulation', 'challenge_usecase must be "simulation"'

        # collect results as dataframe
        df_results_wind_power = pd.concat([df_pred_ensemble, df_test_targ['targets']], axis=1) 
        df_results_wind_power_variability = pd.concat([df_var_ensemble, df_2stage_test['targets']], axis=1)

        # collect results as dictionary of dictionaries
        results_challenge_dict_simulation = {'previous_lt': end_training_timestamp,
                                        'iteration': iteration,
                                        'wind_power': 
                                            {'predictions': df_results_wind_power, 
                                                'info_contributions': previous_day_results_first_stage,
                                                'best_results': best_results},
                                        'wind_power_variability': 
                                            {'predictions': df_results_wind_power_variability, 
                                                'info_contributions': previous_day_results_second_stage,
                                                'best_results': best_results_var},
                                        'wind_power_ramp': 
                                                {'predictions_outsample': var_pred_outsample_df,
                                                'predictions_insample': var_pred_insample_df}
                                            }
        # save results
        with open(file_info, 'wb') as handle:
            pickle.dump(results_challenge_dict_simulation, handle, protocol=pickle.HIGHEST_PROTOCOL)
        return results_challenge_dict_simulation
    
    else:

        # melt dataframe
        df_pred_ensemble_melt = melt_dataframe(df_pred_ensemble) 

        # collect results as dictionary of dictionaries
        results_challenge_dict = {'previous_lt': end_training_timestamp,
                                    'iteration': iteration,
                                    'wind_power': 
                                        {'predictions': df_pred_ensemble_melt, 
                                            'info_contributions': previous_day_results_first_stage,
                                            'best_results': best_results},
                                    'wind_power_variability': 
                                        {'predictions': df_var_ensemble_melt, 
                                            'info_contributions': previous_day_results_second_stage,
                                            'best_results': best_results_var}
                                        }
        # save results
        with open(file_info, 'wb') as handle:
            pickle.dump(results_challenge_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
        assert  challenge_usecase == 'wind_power' or challenge_usecase == 'wind_power_variability', 'challenge_usecase must be either "wind_power" or "wind_power_variability"'
        return results_challenge_dict[challenge_usecase]['predictions']
    